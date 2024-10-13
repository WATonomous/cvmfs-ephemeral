#!/bin/bash

set -o errexit -o nounset -o pipefail

mkdir /srv/cvmfs
ln -s /srv/cvmfs /var/www/cvmfs
a2enmod headers expires proxy proxy_http
service apache2 start

