#!/usr/bin/env python3

import rclpy
import asyncio
import aiohttp
import base64
import cv2
import numpy as np
import threading
import yaml
from rclpy.node import Node
from sensor_msgs.msg import Image, CameraInfo
from cv_bridge import CvBridge
from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy, HistoryPolicy
from ament_index_python.packages import get_package_share_directory

NUM_REQUESTS = 1

def get_camera_params(filepath: str) -> CameraInfo:
    camera_info = CameraInfo()

    with open(filepath, "r") as f:
        camera_params = yaml.safe_load(f)
        camera_info.width = camera_params["image_width"]
        camera_info.height = camera_params["image_height"]
        camera_info.distortion_model = camera_params["distortion_model"]
        camera_info.d = camera_params["distortion_coefficients"]["data"]
        camera_info.k = camera_params["camera_matrix"]["data"]
        camera_info.r = camera_params["rectification_matrix"]["data"]
        camera_info.p = camera_params["projection_matrix"]["data"]
    return camera_info

class AsyncImagePublisher(Node):
    """
    An asynchronous ROS 2 node that fetches camera images from the Earth Rover SDK
    and publishes them to corresponding ROS topics.

    This node uses `asyncio` and `aiohttp` for efficient, non-blocking HTTP requests
    to fetch front, rear, and map images. The images are then published as
    `sensor_msgs/Image` messages, along with their `sensor_msgs/CameraInfo`.
    """
    def __init__(self):
        super().__init__('async_image_publisher')

        self.declare_parameter("earthrover_sdk_url", "http://host.docker.internal:8000")
        self.declare_parameter("publish_rear_frame", False)
        self.declare_parameter("publish_map_frame", False)

        self.sdk_base_url = self.get_parameter("earthrover_sdk_url").get_parameter_value().string_value
        self.publish_rear_frame = self.get_parameter("publish_rear_frame").get_parameter_value().bool_value
        self.publish_map_frame = self.get_parameter("publish_map_frame").get_parameter_value().bool_value

        self.front_camera_url = f"{self.sdk_base_url}/v2/front"
        self.rear_camera_url = f"{self.sdk_base_url}/v2/rear"
        self.map_camera_url = f"{self.sdk_base_url}/screenshot"

        self.get_logger().info(f"Fetching front images from: {self.front_camera_url}")
        self.get_logger().info(f"Rear frame publishing enabled: {self.publish_rear_frame}")
        self.get_logger().info(f"Map frame publishing enabled: {self.publish_map_frame}")
        
        qos_profile = QoSProfile(
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
            history=HistoryPolicy.KEEP_LAST,
            depth=10
        )

        self.front_pub = self.create_publisher(Image, 'front_camera/image_raw', qos_profile)
        self.front_info_pub = self.create_publisher(CameraInfo, 'front_camera/camera_info', qos_profile)

        self.bridge = CvBridge()

        front_path = f"{get_package_share_directory('earthrovers_vision')}/front_camera.yaml"
        rear_path = f"{get_package_share_directory('earthrovers_vision')}/rear_camera.yaml"
        self.front_info = get_camera_params(front_path)
        self.rear_info = get_camera_params(rear_path)

        self.rear_pub = None
        self.rear_info_pub = None
        self.map_pub = None

        if self.publish_rear_frame:
            self.rear_pub = self.create_publisher(Image, 'rear_camera/image_raw', qos_profile)
            self.rear_info_pub = self.create_publisher(CameraInfo, 'rear_camera/camera_info', qos_profile)

        if self.publish_map_frame:
            self.map_pub = self.create_publisher(Image, 'map_image', qos_profile)

        self.get_logger().info("AsyncImagePublisher node has been initialized.")

        # The asyncio event loop runs in a separate thread to avoid blocking the
        # rclpy.spin() call, allowing ROS services and other callbacks to function.
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.run_async_loop, daemon=True).start()

    def run_async_loop(self):
        """
        Runs the asyncio event loop in a dedicated thread.
        """
        asyncio.set_event_loop(self.loop)
        self.loop.create_task(
            self.fetch_and_publish_stream(
                camera_url=self.front_camera_url,
                image_key="front_frame",
                publisher=self.front_pub,
                info=self.front_info,
                info_publisher=self.front_info_pub,
                frame_id="front_camera_link",
            )
        )
        if self.publish_rear_frame:
            self.loop.create_task(
                self.fetch_and_publish_stream(
                    camera_url=self.rear_camera_url,
                    image_key="rear_frame",
                    publisher=self.rear_pub,
                    info=self.rear_info,
                    info_publisher=self.rear_info_pub,
                    frame_id="rear_camera_link",
                )
            )
        if self.publish_map_frame:
            self.loop.create_task(
                self.fetch_and_publish_stream(
                    camera_url=self.map_camera_url,
                    image_key="map_frame",
                    publisher=self.map_pub,
                    info=None,
                    info_publisher=None,
                    frame_id="base_link",
                )
            )
        self.loop.run_forever()

    # async def fetch_images(self):
    #     async with aiohttp.ClientSession() as session:
    #         while rclpy.ok():
    #             tasks = [session.get(self.camera_url) for _ in range(NUM_REQUESTS)]
    #             for task in asyncio.as_completed(tasks):
    #                 try:
    #                     response = await task
    #                 except Exception as e:
    #                     self.get_logger().error(f"Request error: {e}")
    #                     continue
    #                 if response.status == 200:
    #                     data = await response.json()
    #                     await self.image_queue.put(data)
    #                 else:
    #                     self.get_logger().warn(f"Failed to fetch image, status code: {response.status}")

    async def fetch_and_publish_stream(self, camera_url: str, image_key: str, publisher, info, info_publisher, frame_id: str):
        """
        Continuously fetches a single stream and publishes it.
        Each enabled stream runs independently so front publishing is not blocked.
        """
        self.get_logger().info(f"Starting '{image_key}' fetch loop from: {camera_url}")
        async with aiohttp.ClientSession() as session:
            while rclpy.ok():
                try:
                    async with session.get(camera_url) as response:
                        if response.status == 200:
                            data = await response.json()
                            if image_key in data:
                                timestamp = data["timestamp"]
                                timestamp = rclpy.time.Time(seconds=timestamp).to_msg()
                                await self.process_image(
                                    image_key,
                                    data,
                                    timestamp,
                                    publisher,
                                    info,
                                    info_publisher,
                                    frame_id,
                                )
                            else:
                                self.get_logger().debug(
                                    f"Response from {camera_url} did not include '{image_key}'."
                                )
                        else:
                            self.get_logger().warn(f"Failed to fetch image, status code: {response.status}")
                except Exception as e:
                    self.get_logger().error(f"Request error: {e}")
                    await asyncio.sleep(1.0)

    async def process_image(self, key: str, data: dict, timestamp, publisher, info, info_publisher, frame_id: str):
        self.get_logger().debug(f"Processing image for '{key}'")
        loop = asyncio.get_running_loop()
        # decoding
        frame = await loop.run_in_executor(None, self.decode_image, data[key])
        if frame is None:
            return
        if key == "map_frame":
            map_msg = self.bridge.cv2_to_imgmsg(frame, encoding="bgr8")
            map_msg.header.stamp = timestamp
            map_msg.header.frame_id = frame_id
            publisher.publish(map_msg)
        else:
            self.publish_image(frame, publisher, info, info_publisher, timestamp, frame_id)



    def decode_image(self, base64_str):
        try:
            img_data = base64.b64decode(base64_str)
            np_arr = np.frombuffer(img_data, np.uint8)
            frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
            return frame
        except Exception as e:
            self.get_logger().error(f"Error decoding image: {e}")
            return None

    def publish_image(self, frame, img_pub, info, info_pub, timestamp, frame_id):
        if frame is not None:
            msg = self.bridge.cv2_to_imgmsg(frame, encoding="bgr8")
            msg.header.stamp = timestamp
            msg.header.frame_id = frame_id
            img_pub.publish(msg)
            self.get_logger().debug(f"Published image to '{img_pub.topic}' with timestamp {timestamp.sec}.{timestamp.nanosec}")

            info.header.stamp = timestamp
            info.header.frame_id = frame_id
            info_pub.publish(info)

def main(args=None):
    rclpy.init(args=args)
    node = AsyncImagePublisher()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main()