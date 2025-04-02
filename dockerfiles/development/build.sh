#!/bin/bash
SCRIPT_DIR=$(dirname $(readlink -f "$0"))
PKG_DIR=$(realpath $SCRIPT_DIR/../../)
echo $SCRIPT_DIR
docker build -t earthrovers_development -f $SCRIPT_DIR/Dockerfile $PKG_DIR