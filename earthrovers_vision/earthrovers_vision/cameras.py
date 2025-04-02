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
    def __init__(self):
        super().__init__('async_image_publisher')

        self.declare_parameter("earthrover_sdk_url", "http://host.docker.internal:8000")
        self.camera_url = self.get_parameter("earthrover_sdk_url").get_parameter_value().string_value+"/v2/screenshot"
        
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

        self.image_queue = asyncio.Queue()

        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.run_async_loop, daemon=True).start()

    def run_async_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.create_task(self.fetch_images())
        self.loop.create_task(self.publish_images())
        self.loop.run_forever()

    async def fetch_images(self):
        async with aiohttp.ClientSession() as session:
            while rclpy.ok():
                try:
                    async with session.get(self.camera_url) as response:
                        if response.status == 200:
                            data = await response.json()
                            await self.image_queue.put(data)
                        else:
                            self.get_logger().warn(f"Failed to fetch image, status code: {response.status}")
                except Exception as e:
                    self.get_logger().error(f"Error fetching image: {e}")

    async def publish_images(self):
        while rclpy.ok():
            data = await self.image_queue.get()

            timestamp = self.get_clock().now().to_msg()

            if "front_frame" in data:
                front_frame = self.decode_image(data["front_frame"])
                self.publish_image(front_frame, self.front_pub, self.front_info, self.front_info_pub, timestamp, "front_camera_link")

            if "rear_frame" in data:
                rear_frame = self.decode_image(data["rear_frame"])
                self.publish_image(rear_frame, self.rear_pub, self.rear_info, self.rear_info_pub, timestamp, "rear_camera_link")

            if "map_frame" in data:
                map_frame = self.decode_image(data["map_frame"])
                if map_frame is not None:
                    map_msg = self.bridge.cv2_to_imgmsg(map_frame, encoding="bgr8")
                    map_msg.header.stamp = timestamp
                    map_msg.header.frame_id = "base_link"
                    self.map_pub.publish(map_msg)

            self.image_queue.task_done()

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