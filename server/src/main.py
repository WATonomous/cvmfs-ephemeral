import json
import random
import string
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock

import typer
import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import HTTPException, UploadFile
from fastapi.responses import FileResponse
from slugify import slugify
from typing_extensions import Annotated
from watcloud_utils.fastapi import WATcloudFastAPI, FastAPI
from watcloud_utils.logging import logger, set_up_logging
from watcloud_utils.typer import app

set_up_logging()

TTL_FILENAME = "ttl.json"
DEFAULT_TTL_S = 7200

FILENAME_BLACKLIST = [TTL_FILENAME]

@app.command()
def init_cvmfs_repo(
    repo_name: Annotated[str, typer.Argument(help="Name of the CVMFS repo. CVMFS requires this to be an FQDN.")],
    volatile: Annotated[bool, typer.Option(help="Whether the repo is volatile or not. If True, the repo will be created (cvmfs_server mkfs) with the -v flag.")] = True,
    enable_garbage_collection: Annotated[bool, typer.Option(help="Whether to enable garbage collection for the repo.")] = True,
    disable_auto_tag: Annotated[bool, typer.Option(help="Whether to disable auto-tagging for the repo.")] = True,
    compression_algorithm: Annotated[str, typer.Option(help="Compression algorithm to use for the repo.")] = "none",
    file_mbyte_limit: Annotated[int, typer.Option(help="Maximum file size in MiB that can be uploaded to the repo.")] = 4096,
):
    """
    Initialize a CVMFS repo.

    Docs: https://cvmfs.readthedocs.io/en/stable/cpt-repo.html
    """
    print(f"Initializing CVMFS repo: {repo_name}")

    # Make apache2 serve cvmfs repos
    Path("/srv/cvmfs").mkdir(parents=True, exist_ok=True)
    if not Path("/var/www/cvmfs").exists():
        Path("/var/www/cvmfs").symlink_to("/srv/cvmfs")

    # Enable apache2 modules
    res = subprocess.run(["a2enmod", "headers", "expires", "proxy", "proxy_http"], check=True)
    if res.returncode != 0:
        sys.exit(f"Failed to enable apache2 modules (exit code: {res.returncode})")

    # Start apache2 service
    res = subprocess.run(["service", "apache2", "start"], check=True)
    if res.returncode != 0:
        sys.exit(f"Failed to start apache2 service (exit code: {res.returncode})")

    # Run cvmfs_server mkfs
    res = subprocess.run(
        ["cvmfs_server", "mkfs", "-o", "root", "-Z", compression_algorithm]
        + (["-v"] if volatile else []) 
        + (["-z"] if enable_garbage_collection else [])
        + (["-g"] if disable_auto_tag else [])
        + [repo_name],
        check=True
    )
    if res.returncode != 0:
        sys.exit(f"Failed to run cvmfs_server mkfs (exit code: {res.returncode})")

    # Populate repo configuration
    repo_config_path = Path(f"/etc/cvmfs/repositories.d/{repo_name}/server.conf")
    with open(repo_config_path, "a") as f:
        f.write("\n")
        f.write(f"CVMFS_FILE_MBYTE_LIMIT={file_mbyte_limit}\n")

    # Make the public key and certificate available via HTTP
    # Useful for clients and publishers:
    # https://cvmfs.readthedocs.io/en/stable/cpt-repository-gateway.html#example-procedure
    Path("/var/www/html/cvmfs-meta").mkdir(parents=True, exist_ok=True)
    Path(f"/var/www/html/cvmfs-meta/{repo_name}.pub").symlink_to(f"/etc/cvmfs/keys/{repo_name}.pub")
    Path(f"/var/www/html/cvmfs-meta/{repo_name}.crt").symlink_to(f"/etc/cvmfs/keys/{repo_name}.crt")

    # Configure cvmfs-gateway
    gateway_key_path = Path(f"/etc/cvmfs/keys/{repo_name}.gw")
    if gateway_key_path.exists():
        print(f"Gateway key already exists for repo: {repo_name}. Reusing the existing key.")
    else:
        print(f"Generating gateway key for repo: {repo_name}")
        gateway_key_name = slugify(f"{repo_name}_root")
        gateway_key = ''.join(random.choices(string.ascii_uppercase + string.digits, k=32))
        # generate a random string
        gateway_key_path.write_text(f"plain_text {gateway_key_name} {gateway_key}")

    gateway_repo_config_path = Path("/etc/cvmfs/gateway/repo.json")
    gateway_repo_config = json.loads(gateway_repo_config_path.read_text())
    gateway_repo_config["repos"].append(repo_name)
    gateway_repo_config_path.write_text(json.dumps(gateway_repo_config, indent=4))

    # Restart cvmfs-gateway
    res = subprocess.run(["service", "cvmfs-gateway", "restart"], check=True)
    if res.returncode != 0:
        sys.exit(f"Failed to restart cvmfs-gateway service (exit code: {res.returncode})")

    print(f"Successfully initialized CVMFS repo: {repo_name}")
    print(f"The public key is available via HTTP at GET /cvmfs-meta/{repo_name}.pub")

@asynccontextmanager
async def fastapi_lifespan(app: FastAPI):
    """
    This function wraps the FastAPI app in a lifespan context manager.
    i.e. it allows us to run code when the app starts and stops.
    """
    try:
        scheduler.start()
        # Run housekeeping every minute
        scheduler.add_job(housekeeping, CronTrigger.from_crontab("* * * * *"))
        yield
    finally:
        scheduler.shutdown()

scheduler = BackgroundScheduler()
fastapi_app = WATcloudFastAPI(logger=logger, lifespan=fastapi_lifespan)
transaction_lock = Lock()

@fastapi_app.post("/repos/{repo_name}")
async def upload(repo_name: str, file: UploadFile, overwrite: bool = False, ttl_s: int = DEFAULT_TTL_S):
    logger.info(f"Uploading file: {file.filename} (content_type: {file.content_type}, ttl_s: {ttl_s}) to repo: {repo_name}")

    if file.filename in FILENAME_BLACKLIST:
        raise HTTPException(status_code=400, detail=f"Filename {file.filename} is not allowed")

    # check if repo exists
    if not Path(f"/cvmfs/{repo_name}").exists():
        raise HTTPException(status_code=404, detail=f"Repo {repo_name} does not exist")

    file_path = Path(f"/cvmfs/{repo_name}/{file.filename}")
    if not overwrite and file_path.exists():
        raise HTTPException(status_code=409, detail=f"File {file.filename} already exists")

    expires_at = time.time() + ttl_s
    ttl_path = Path(f"/cvmfs/{repo_name}/{TTL_FILENAME}")

    with transaction_lock:
        # start transaction
        subprocess.run(["cvmfs_server", "transaction", repo_name], check=True)

        try:
            # Remove existing file
            if file_path.exists():
                file_path.unlink()

            # Upload file
            with file_path.open("wb") as f:
                upload_start = time.perf_counter()
                f.write(await file.read())
                upload_end = time.perf_counter()

            # Update TTL
            ttl_obj = json.loads(ttl_path.read_text()) if ttl_path.exists() else {}
            ttl_obj[file.filename] = {"expires_at": expires_at}
            ttl_path.write_text(json.dumps(ttl_obj))

            logger.info(f"Uploaded file: {file.filename} (content_type: {file.content_type}). Took {upload_end - upload_start:.2f}s")
        except Exception as e:
            logger.error(f"Failed to upload file: {file.filename} (content_type: {file.content_type})")
            logger.exception(e)
            # abort transaction
            subprocess.run(["cvmfs_server", "abort", repo_name, "-f"], check=True)
            raise HTTPException(status_code=500, detail="Failed to upload file")

        # publish transaction
        publish_start = time.perf_counter()
        subprocess.run(["cvmfs_server", "publish", repo_name], check=True)
        publish_end = time.perf_counter()

        logger.info(f"Published transaction for repo: {repo_name} with file: {file.filename} (content_type: {file.content_type}). Took {publish_end - publish_start:.2f}s. Expires at: {expires_at}")

        notify(repo_name)

    return {
        "filename": file.filename,
        "content_type": file.content_type,
        "expires_at": expires_at,
        "upload_time_s": upload_end - upload_start,
        "publish_time_s": publish_end - publish_start,
    }


@app.command()
@fastapi_app.post("/repos/{repo_name}/{file_name}/ttl")
async def update_ttl(repo_name: str, file_name: str, ttl_s: int):
    logger.info(f"Updating TTL for file: {file_name} in repo: {repo_name}")

    if file_name in FILENAME_BLACKLIST:
        raise HTTPException(status_code=400, detail=f"Filename {file_name} is not allowed")

    file_path = Path(f"/cvmfs/{repo_name}/{file_name}")
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"File {file_name} does not exist in repo {repo_name}")

    ttl_path = Path(f"/cvmfs/{repo_name}/{TTL_FILENAME}")

    with transaction_lock:
        # start transaction
        subprocess.run(["cvmfs_server", "transaction", repo_name], check=True)

        try:
            # Update TTL
            ttl_obj = json.loads(ttl_path.read_text())
            ttl_obj[file_name] = {"expires_at": time.time() + ttl_s}
            ttl_path.write_text(json.dumps(ttl_obj))

            logger.info(f"Updated TTL for file: {file_name} in repo: {repo_name}")
        except Exception as e:
            logger.error(f"Failed to update TTL for file: {file_name} in repo: {repo_name}")
            logger.exception(e)
            # abort transaction
            subprocess.run(["cvmfs_server", "abort", repo_name, "-f"], check=True)
            raise HTTPException(status_code=500, detail=f"Failed to update TTL for file: {file_name}: {e}")

        # publish transaction
        subprocess.run(["cvmfs_server", "publish", repo_name], check=True)
        notify(repo_name)

    return {"filename": file_name, "ttl_s": ttl_s}


@app.command()
@fastapi_app.get("/repos/{repo_name}/{file_name}")
async def download(repo_name: str, file_name: str):
    logger.info(f"Downloading file: {file_name} from repo: {repo_name}")

    file_path = Path(f"/cvmfs/{repo_name}/{file_name}")
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"File {file_name} does not exist in repo {repo_name}")

    return FileResponse(file_path)

@app.command()
@fastapi_app.get("/repos/{repo_name}")
async def list_files(repo_name: str):
    logger.info(f"Listing files in repo: {repo_name}")

    repo_path = Path(f"/cvmfs/{repo_name}")
    if not repo_path.exists():
        raise HTTPException(status_code=404, detail=f"Repo {repo_name} does not exist")

    return {"files": [file.name for file in repo_path.iterdir() if file.is_file()]}

@app.command()
@fastapi_app.delete("/repos/{repo_name}/{file_name}")
async def delete_file(repo_name: str, file_name: str):
    logger.info(f"Deleting file: {file_name} from repo: {repo_name}")

    if file_name in FILENAME_BLACKLIST:
        raise HTTPException(status_code=400, detail=f"Filename {file_name} is not allowed")

    file_path = Path(f"/cvmfs/{repo_name}/{file_name}")
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"File {file_name} does not exist in repo {repo_name}")

    ttl_path = Path(f"/cvmfs/{repo_name}/{TTL_FILENAME}")

    with transaction_lock:
        # start transaction
        subprocess.run(["cvmfs_server", "transaction", repo_name], check=True)

        try:
            # Remove file
            file_path.unlink()

            # Update TTL
            ttl_obj = json.loads(ttl_path.read_text())
            del ttl_obj[file_name]
            ttl_path.write_text(json.dumps(ttl_obj))

            logger.info(f"Deleted file: {file_name} from repo: {repo_name}")
        except Exception as e:
            logger.error(f"Failed to delete file: {file_name} from repo: {repo_name}")
            logger.exception(e)
            # abort transaction
            subprocess.run(["cvmfs_server", "abort", repo_name, "-f"], check=True)
            raise HTTPException(status_code=500, detail="Failed to delete file")

        # publish transaction
        subprocess.run(["cvmfs_server", "publish", repo_name], check=True)
        notify(repo_name)

    return {"filename": file_name}

@app.command()
@fastapi_app.post("/repos/{repo_name}/clean")
def clean(repo_name: str):
    """
    Clean up expired files in the repo.
    """
    logger.info(f"Cleaning up expired files in repo: {repo_name}")

    ttl_path = Path(f"/cvmfs/{repo_name}/{TTL_FILENAME}")
    if not ttl_path.exists():
        logger.info(f"No TTL file found in repo: {repo_name}. Skipping clean up.")
        return {"message": "No TTL file found. Skipping clean up."}

    cleaned = 0
    errors = 0

    with transaction_lock:
        # start transaction
        subprocess.run(["cvmfs_server", "transaction", repo_name], check=True)

        try:
            ttl_obj = json.loads(ttl_path.read_text())
            for file_name, ttl in ttl_obj.copy().items():
                if ttl["expires_at"] < time.time():
                    file_path = Path(f"/cvmfs/{repo_name}/{file_name}")
                    if file_path.exists():
                        file_path.unlink()
                        cleaned += 1
                    else:
                        logger.warning(f"Trying to clean up non-existent file: {file_name} in repo: {repo_name}")
                        errors += 1
                    del ttl_obj[file_name]

            ttl_path.write_text(json.dumps(ttl_obj))

            logger.info(f"Cleaned up expired files in repo: {repo_name}")
        except Exception as e:
            logger.error(f"Failed to clean up expired files in repo: {repo_name}")
            logger.exception(e)
            # abort transaction
            subprocess.run(["cvmfs_server", "abort", repo_name, "-f"], check=True)
            raise HTTPException(status_code=500, detail=f"Failed to clean up expired files: {e}")

        # publish transaction
        subprocess.run(["cvmfs_server", "publish", repo_name], check=True)
        notify(repo_name)

    msg = f"Cleaned up {cleaned} expired files in repo: {repo_name}. Errors: {errors}"
    logger.info(msg)

    return {"message": msg}

@app.command()
@fastapi_app.post("/repos/{repo_name}/notify")
def notify(repo_name: str):
    """
    Use cvmfs-gateway to notify clients about changes in the repo.
    """
    logger.info(f"Notifying clients about changes in repo: {repo_name}")

    if not Path(f"/cvmfs/{repo_name}").exists():
        raise HTTPException(status_code=404, detail=f"Repo {repo_name} does not exist")

    subprocess.run(
        [
            "cvmfs_swissknife",
            "notify",
            # publish
            "-p",
            # notification server URL
            "-u",
            "http://localhost:4929/api/v1",
            # URL of the repository
            "-r",
            f"http://localhost/cvmfs/{repo_name}",
        ],
        check=True,
    )

    return {"message": f"Notified clients about changes in repo {repo_name}"}


@app.command()
@fastapi_app.post("/gc")
def gc():
    """
    Perform garbage collection on all repos.
    """
    with transaction_lock:
        logger.info("Running garbage collection")
        gc_start = time.perf_counter()
        subprocess.run(["cvmfs_server", "gc", "-r", "0", "-f"], check=True)
        gc_end = time.perf_counter()

        logger.info(f"Garbage collection completed. Took {gc_end - gc_start:.2f}s")
        return {"message": "Garbage collection completed", "gc_time_s": gc_end - gc_start}

@app.command()
@fastapi_app.post("/housekeeping")
def housekeeping():
    """
    Clean all repos and perform garbage collection.
    """
    logger.info("Running housekeeping")
    housekeeping_start = time.perf_counter()
    for repo_path in Path("/cvmfs").iterdir():
        if repo_path.is_dir():
            repo_name = repo_path.name
            clean(repo_name)
    gc()
    housekeeping_end = time.perf_counter()

    logger.info(f"Housekeeping completed. Took {housekeeping_end - housekeeping_start:.2f}s")
    return {"message": "Housekeeping completed", "housekeeping_time_s": housekeeping_end - housekeeping_start}

@app.command()
def start_server(port: int = 81):
    uvicorn.run(fastapi_app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    app()
