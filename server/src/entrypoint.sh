#!/bin/bash

set -o errexit -o nounset -o pipefail

mkdir /srv/cvmfs
ln -s /srv/cvmfs /var/www/cvmfs
a2enmod headers expires proxy proxy_http
service apache2 start

# Add cvmfs_server resign command
cvmfs_server resign

# Schedule cvmfs_server resign command to run daily using a cron job
echo "0 0 * * * root cvmfs_server resign" > /etc/cron.d/cvmfs_resign
cron
