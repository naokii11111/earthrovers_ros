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

# Add useful aliases to bashrc
echo "alias start-mission=\"echo '' && curl --location --request POST 'http://localhost:8000/start-mission' && echo '' \"" >> /root/.bash_aliases
echo "alias get-data=\"echo '' && curl --location 'http://localhost:8000/data' && echo '' \"" >> /root/.bash_aliases

# Start a bash shell
exec /bin/bash