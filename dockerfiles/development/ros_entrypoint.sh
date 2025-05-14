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
echo "alias end-mission=\"echo '' && curl --location --request POST 'http://localhost:8000/end-mission' && echo '' \"" >> /root/.bash_aliases
echo "alias get-data=\"echo '' && curl --location 'http://localhost:8000/data' && echo '' \"" >> /root/.bash_aliases
echo "alias launch-zero=\"echo '' && tmuxp load /earthrovers_ws/src/earthrovers_ros/tmuxp_configs/dev_zero.yaml && echo '' \"" >> /root/.bash_aliases
echo "alias launch-mini=\"echo '' && tmuxp load /earthrovers_ws/src/earthrovers_ros/tmuxp_configs/dev_mini.yaml && echo '' \"" >> /root/.bash_aliases

# Start a bash shell
exec /bin/bash