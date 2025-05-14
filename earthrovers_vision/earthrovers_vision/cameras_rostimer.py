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

from rclpy.executors import MultiThreadedExecutor
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup

import requests
import queue

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

class RosTimerImagePublisher(Node):
    def __init__(self):
        super().__init__('async_image_publisher')

        self.declare_parameter("earthrover_sdk_url", "http://host.docker.internal:8000")
        # self.camera_url = self.get_parameter("earthrover_sdk_url").get_parameter_value().string_value+"/v2/screenshot"        
        self.camera_url = self.get_parameter("earthrover_sdk_url").get_parameter_value().string_value+"/v2/front"

        qos_profile = QoSProfile(
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
            history=HistoryPolicy.KEEP_LAST,
            depth=10
        )

        self.front_pub = self.create_publisher(Image, 'front_camera/image_raw', qos_profile)
        self.front_info_pub = self.create_publisher(CameraInfo, 'front_camera/camera_info', qos_profile)

        self.rear_pub = self.create_publisher(Image, 'rear_camera/image_raw', qos_profile)
        self.rear_info_pub = self.create_publisher(CameraInfo, 'rear_camera/camera_info', qos_profile)

        self.map_pub = self.create_publisher(Image, 'map_image', qos_profile)

        self.bridge = CvBridge()

        front_path = f"{get_package_share_directory('earthrovers_vision')}/front_camera.yaml"
        rear_path = f"{get_package_share_directory('earthrovers_vision')}/rear_camera.yaml"
        self.front_info = get_camera_params(front_path)
        self.rear_info = get_camera_params(rear_path)

        self.image_queue = queue.Queue(maxsize=10)
        self._image_fetch_timer = self.create_timer(1 / 4, self.fetch_images, callback_group=MutuallyExclusiveCallbackGroup())
        self._image_publish_timer = self.create_timer(0.02, self.publish_images, callback_group=MutuallyExclusiveCallbackGroup())

    def fetch_images(self):
        """
        Fetch images synchronously with requests
        """
        response = requests.get(self.camera_url)
        if response.status_code == 200:
            data = response.json()
            self.image_queue.put(data)
        else:
            self.get_logger().warn(f"Failed to fetch image, status code: {response.status_code}")
    
    def publish_images(self):
        """
        Publish images synchronously
        """
        try:
            data = self.image_queue.get_nowait()
            if data is None:
                return
            timestamp = self.get_clock().now().to_msg()

            for key, frame_id, publisher, info, info_publisher in [
                ("front_frame", "front_camera_link", self.front_pub, self.front_info, self.front_info_pub),
                ("rear_frame", "rear_camera_link", self.rear_pub, self.rear_info, self.rear_info_pub),
                ("map_frame", "base_link", self.map_pub, None, None)
            ]:
                if key in data:
                    frame = self.decode_image(data[key])
                    if frame is None:
                        continue
                if key == "map_frame":
                    map_msg = self.bridge.cv2_to_imgmsg(frame, encoding="bgr8")
                    map_msg.header.stamp = timestamp
                    map_msg.header.frame_id = frame_id
                    publisher.publish(map_msg)
                else:
                    self.publish_image(frame, publisher, info, info_publisher, timestamp, frame_id)
        except queue.Empty:
            pass

    # def run_async_loop(self):
    #     asyncio.set_event_loop(self.loop)
    #     self.loop.create_task(self.fetch_images())
    #     self.loop.create_task(self.publish_images())
    #     self.loop.run_forever()

    async def _fetch_images_async(self):
        async with aiohttp.ClientSession() as session:
            while rclpy.ok():
                # making requests refferign the number of NUM_REQUESTS
                tasks = [session.get(self.camera_url) for _ in range(1)]
                # MODIFIED(This can solve burst problem)
                for task in asyncio.as_completed(tasks):
                    try:
                        response = await task
                    except Exception as e:
                        self.get_logger().error(f"Request error: {e}")
                        continue
                    if response.status == 200:
                        data = await response.json()
                        await self.image_queue.put(data)
                    else:
                        self.get_logger().warn(f"Failed to fetch image, status code: {response.status}")
                # 次
                # await asyncio.sleep(0.2)


    async def process_image_async(self, key: str, data: dict, timestamp, publisher, info, info_publisher, frame_id: str):
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


    async def publish_images_async(self):
        try:
            data = await self.image_queue.get_nowait()
            if data is None:
                return
            timestamp = self.get_clock().now().to_msg()

            # MO
            if "front_frame" in data:
                asyncio.create_task(
                    self.process_image("front_frame", data, timestamp,
                                    self.front_pub, self.front_info, self.front_info_pub,
                                    "front_camera_link")
                )
            if "rear_frame" in data:
                asyncio.create_task(
                    self.process_image("rear_frame", data, timestamp,
                                    self.rear_pub, self.rear_info, self.rear_info_pub,
                                    "rear_camera_link")
                )
            if "map_frame" in data:
                asyncio.create_task(
                    self.process_image("map_frame", data, timestamp,
                                    self.map_pub, None, None,
                                    "base_link")
                )

            self.image_queue.task_done()
        except asyncio.QueueEmpty:
            pass

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

            info.header.stamp = timestamp
            info.header.frame_id = frame_id
            info_pub.publish(info)

def main(args=None):
    rclpy.init(args=args)
    node = RosTimerImagePublisher()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main()