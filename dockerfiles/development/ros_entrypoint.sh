#!/bin/bash

# Install dependencies
cd /earthrovers_ws && \
sudo apt-get update && \
rosdep update --rosdistro $ROS_DISTRO && \
rosdep install --from-paths src -y -r --ignore-src

# Build workspace
source /opt/ros/humble/setup.bash && \
cd /earthrovers_ws && \
colcon build
source /earthrovers_ws/install/setup.bash
exec /bin/bash