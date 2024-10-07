# cvmfs-ephemeral

A CVMFS server meant fo storing ephemeral data. The main features are:
- Stateless (except for the keys in `/etc/cvmfs/keys`)
- Integrated notification system ([via cvmfs-gateway](https://cvmfs.readthedocs.io/en/stable/cpt-notification-system.html)) for clients to subscribe to changes in addition to the standard TTL-based polling

These features make it suitable for storing short-lived artifacts in CI/CD pipelines.

Coming soon:
- [ ] File upload API
- [ ] Garbage collection
- [ ] Better documentation

### Manual Testing

Server:

```
docker compose run --service-ports cvmfs-server

python3 main.py init-cvmfs-repo cvmfs-server.example.local
```

Client:

```bash
docker run --rm -it \
    --name cvmfs \
    -e CVMFS_CLIENT_PROFILE=single \
    -e CVMFS_REPOSITORIES=cvmfs-server.example.local \
    --cap-add SYS_ADMIN \
    --security-opt apparmor:unconfined \
    --device /dev/fuse \
    --entrypoint sh \
    registry.cern.ch/cvmfs/service:2.11.2-1

wget thor-slurm1.cluster.watonomous.ca:8080/cvmfs-meta/cvmfs-server.example.local.pub -O /etc/cvmfs/keys/cvmfs-server.example.local.pub
cat <<EOF > /etc/cvmfs/config.d/cvmfs-server.example.local.conf
# For some reason we can't use @fprn@ here. The client doesn't appear to do the substitution.
CVMFS_SERVER_URL=http://thor-slurm1.cluster.watonomous.ca:8080/cvmfs/cvmfs-server.example.local
CVMFS_NOTIFICATION_SERVER=http://thor-slurm1.cluster.watonomous.ca:4929/api/v1
CVMFS_KEYS_DIR=/etc/cvmfs/keys/
# Makes the client check for updates more frequently. In minutes.
CVMFS_MAX_TTL=1
# Required. Otherwise we get "failed to discover HTTP proxy servers (23 - proxy auto-discovery failed)" on our custom cvmfs-server.
CVMFS_HTTP_PROXY=DIRECT
EOF
/usr/bin/mount_cvmfs.sh


docker exec -it cvmfs sh -c "while true; do date; ls -alh /cvmfs/cvmfs-server.example.local; sleep 5; done"

docker exec -it cvmfs tail -f /var/log/cvmfs.log
```

### Notifications

```bash
# publish
cvmfs_swissknife notify -p -u http://localhost:4929/api/v1 -r http://localhost/cvmfs/cvmfs-server.example.local
i=${i:-0}; i=$((i+1)); cvmfs_server transaction && echo $i > /cvmfs/cvmfs-server.example.local/test-$i.txt && cvmfs_server publish && cvmfs_swissknife notify -p -u http://localhost:4929/api/v1 -r http://localhost/cvmfs/cvmfs-server.example.local && echo "Published $i"

# subscribe
cvmfs_swissknife notify -s -u http://localhost:4929/api/v1 -t cvmfs-server.example.local -c
```

https://cvmfs.readthedocs.io/en/stable/cpt-notification-system.html

Note that if the notification subscription breaks (e.g. when the server goes down), it doesn't appear to recover without a client restart:

```
Mon Oct  7 22:36:30 2024 (cvmfs-server.example.local) SubscriberSSE - event loop finished with error: 7. Reply:

Mon Oct  7 22:36:30 2024 (cvmfs-server.example.local) SubscriberSupervisor - Subscription failed. Retrying.
```

This is because the retry has no backoff and the retry limit is reached almost immediately:
- https://github.com/cvmfs/cvmfs/blob/669309e4bb84894acfb23c316ab6b7a07c4a34bc/cvmfs/notification_client.cc#L154-L159
- https://github.com/cvmfs/cvmfs/blob/669309e4bb84894acfb23c316ab6b7a07c4a34bc/cvmfs/notify/subscriber_supervisor.cc#L33
- https://github.com/cvmfs/cvmfs/blob/669309e4bb84894acfb23c316ab6b7a07c4a34bc/cvmfs/supervisor.cc

It's definitely a bug, because in the test suite, the retry limit appears to be used for tasks that don't immediately fail:
- https://github.com/cvmfs/cvmfs/blob/669309e4bb84894acfb23c316ab6b7a07c4a34bc/test/unittests/t_supervisor.cc#L28

Also, the supervisor appears to be made for one-off tasks, not long-running tasks like the subscriber:
- https://github.com/cvmfs/cvmfs/blob/669309e4bb84894acfb23c316ab6b7a07c4a34bc/cvmfs/supervisor.cc#L28

References:
- [Creating a Repository (Stratum 0)](https://cvmfs.readthedocs.io/en/stable/cpt-repo.html)
- [Stratum 0 and client tutorial](https://cvmfs-contrib.github.io/cvmfs-tutorial-2021/02_stratum0_client/)
- [Server Spool Area of a Repository (Stratum0)](https://cvmfs.readthedocs.io/en/stable/apx-serverinfra.html#server-spool-area-of-a-repository-stratum0)
- [The CernVM-FS Notification System (Experimental)](https://cvmfs.readthedocs.io/en/stable/cpt-notification-system.html)
- [TOWARDS A RESPONSIVE CVMFS ARCHITECTURE](https://indico.cern.ch/event/587955/contributions/2937405/attachments/1682388/2703315/radu_popescu_chep_2018.pdf)
- [Towards a Serverless CernVM-FS](https://indico.cern.ch/event/587955/contributions/3012720/attachments/1685212/2711599/cvmfs-chep18.pdf)
