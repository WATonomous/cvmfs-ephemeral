import json
import random
import string
import subprocess
import sys
from pathlib import Path

from slugify import slugify
from watcloud_utils.typer import app


@app.command()
def init_cvmfs_repo(repo_name: str):
    print(f"Initializing CVMFS repo: {repo_name}")

    # Make apache2 serve cvmfs repos
    Path("/srv/cvmfs").mkdir(parents=True, exist_ok=True)
    if not Path("/var/www/cvmfs").exists():
        Path("/var/www/cvmfs").symlink_to("/srv/cvmfs")

    # Enable apache2 modules
    res = subprocess.run(["a2enmod", "headers", "expires", "proxy", "proxy_http"])
    if res.returncode != 0:
        sys.exit(f"Failed to enable apache2 modules (exit code: {res.returncode})")

    # Start apache2 service
    res = subprocess.run(["service", "apache2", "start"])
    if res.returncode != 0:
        sys.exit(f"Failed to start apache2 service (exit code: {res.returncode})")

    # Run cvmfs_server mkfs
    res = subprocess.run(["cvmfs_server", "mkfs", "-o", "root", "-Z", "none", repo_name])
    if res.returncode != 0:
        sys.exit(f"Failed to run cvmfs_server mkfs (exit code: {res.returncode})")

    # Make the public key available for clients
    Path("/var/www/html/cvmfs-meta").mkdir(parents=True, exist_ok=True)
    Path(f"/var/www/html/cvmfs-meta/{repo_name}.pub").symlink_to(f"/etc/cvmfs/keys/{repo_name}.pub")

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
    res = subprocess.run(["service", "cvmfs-gateway", "restart"])
    if res.returncode != 0:
        sys.exit(f"Failed to restart cvmfs-gateway service (exit code: {res.returncode})")

    print(f"Successfully initialized CVMFS repo: {repo_name}")
    print(f"The public key is available via HTTP at GET /cvmfs-meta/{repo_name}.pub")

@app.command()
def start_server():
    print("Starting server")
    while True:
        pass


if __name__ == "__main__":
    app()
