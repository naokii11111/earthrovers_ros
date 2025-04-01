#!/usr/bin/env python3

import rclpy
import asyncio
import aiohttp
import base64
import cv2
import numpy as np
from rclpy.node import Node
from sensor_msgs.msg import Image
from cv_bridge import CvBridge

CAMERA_URL = "http://host.docker.internal:8000/v2/front"

class AsyncImagePublisher(Node):
    def __init__(self):
        super().__init__('async_image_publisher')
        self.publisher = self.create_publisher(Image, 'camera/image_raw', 10)
        self.bridge = CvBridge()
        self.image_queue = asyncio.Queue()  # Queue to store fetched images
        self.get_logger().info("Starting async image publisher")

        self.loop = asyncio.get_event_loop()
        self.loop.create_task(self.fetch_images())  # Producer
        self.loop.create_task(self.publish_images())  # Consumer
        self.loop.run_forever()

    async def fetch_images(self):
        """Asynchronously fetch images and put them in the queue."""
        self.get_logger().info("Starting fetch loop")
        async with aiohttp.ClientSession() as session:
            while rclpy.ok():
                try:
                    self.get_logger().info("Fetching image")
                    async with session.get(CAMERA_URL) as response:
                        if response.status == 200:
                            data = await response.json()
                            await self.image_queue.put(data)  # Push image to queue
                        else:
                            self.get_logger().warn(f"Failed to fetch image, status code: {response.status}")
                except Exception as e:
                    self.get_logger().error(f"Error fetching image: {e}")

                # await asyncio.sleep(0.1)  # Allow other tasks to run

    async def publish_images(self):
        """Continuously publish images from the queue."""
        self.get_logger().info("Starting publish loop")
        while rclpy.ok():
            data = await self.image_queue.get()  # Wait for an image from the queue
            frame = self.decode_image(data["front_frame"])
            self.publish_image(frame)
            self.image_queue.task_done()  # Mark task as done

    def decode_image(self, base64_str):
        """Decode base64-encoded image to OpenCV format."""
        try:
            img_data = base64.b64decode(base64_str)
            np_arr = np.frombuffer(img_data, np.uint8)
            frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
            return frame
        except Exception as e:
            self.get_logger().error(f"Error decoding image: {e}")
            return None

    def publish_image(self, frame):
        """Convert OpenCV image to ROS Image message and publish it."""
        msg = self.bridge.cv2_to_imgmsg(frame, encoding="bgr8")
        self.publisher.publish(msg)
        self.get_logger().info("Published image frame")

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