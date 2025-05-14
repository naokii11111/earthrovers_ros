#!/bin/bash

SCRIPT=$(readlink -f "$0")
EARTHROVERS_WS=$(realpath $(dirname $SCRIPT)/../../../../)
rocker --persist-image --x11 --privileged --network host --ipc host --name earthrovers_ros --volume $EARTHROVERS_WS:/earthrovers_ws -- earthrovers_development /opt/ros_entrypoint.sh