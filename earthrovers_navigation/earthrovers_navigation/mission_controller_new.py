"""Node that gets waypoints from the EarthRovers SDK and publishes them as a
GeoPath message. It also uses the SDK response to figure out which waypoint
should be followed next.
"""

import time
import rclpy
from rclpy.node import Node
from rclpy.executors import MultiThreadedExecutor
import requests
from geographic_msgs.msg import GeoPoseStamped, GeoPath
from sensor_msgs.msg import NavSatFix

from sklearn.metrics.pairwise import haversine_distances
import numpy as np

class WaypointReceiverNode(Node):
    """Node that receives waypoints from the EarthRovers SDK to check for
    checkpoints and to continuously call checkpoint reached to scan it.
    """

    def __init__(self):
        super().__init__("waypoint_receiver")

        # Make publisher for GeoPath
        self.waypoints_pub = self.create_publisher(GeoPath, "checkpoints_gps", 10)

        self.checkpoints_list = None
        self.latest_scanned_checkpoint = -1

        # Subscribe to gps for the current position
        self.current_gps = None
        self._gps_sub = self.create_subscription(
            NavSatFix,
            '/gps',
            self._gps_callback,
            10
        )


        # Make timers
        self.reached_last_checkpoint = False
        check_checkpoint_reached_period = 1.0
        self.create_timer(check_checkpoint_reached_period, self.check_checkpoint_reached)

        self.declare_parameter("earthrover_sdk_url", "http://host.docker.internal:8000")
        self.earthrover_sdk_url = self.get_parameter("earthrover_sdk_url").get_parameter_value().string_value
        self.get_checkpoints_list()

    def _gps_callback(self, msg):
        self.current_gps = msg

    def get_checkpoints_list(self):
        while self.checkpoints_list is None:
            try:
                checkpoints_list_response = requests.get(f"{self.earthrover_sdk_url}/checkpoints-list")
                # Parse the checkpoints from the response and create a list of
                # of Waypoint messages.
                if checkpoints_list_response.status_code == 200:
                    try:
                        checkpoints_list_json = checkpoints_list_response.json()
                    except:
                        self.get_logger().warn("Failed to parse checkpoints list response.")
                        checkpoints_list_json = None

                    # Get the checkpoints and create waypoint message from each
                    if checkpoints_list_json is not None:
                        checkpoint_dicts = checkpoints_list_json["checkpoints_list"]
                        self.latest_scanned_checkpoint = checkpoints_list_json["latest_scanned_checkpoint"]
                        print("Checkpoint list", checkpoints_list_json)

                        checkpoints_list = GeoPath()
                        for checkpoint_dict in checkpoint_dicts:
                            geoposes = GeoPoseStamped()
                            geoposes.pose.position.latitude = float(checkpoint_dict["latitude"])
                            geoposes.pose.position.longitude = float(checkpoint_dict["longitude"])
                            geoposes.pose.position.altitude = 0.0
                            checkpoints_list.poses.append(geoposes)
                        self.checkpoints_list = checkpoints_list

            except requests.exceptions.RequestException as e:
                self.get_logger().error(f"Failed to get checkpoints list: {e}")
                raise Exception("Failed to get checkpoints list.")

    def check_checkpoint_reached(self):

        if self.checkpoints_list is None:
            self.get_logger().warn("Checkpoints list is None. Cannot check checkpoint reached.")
            return
        
        if self.current_gps is None:
            self.get_logger().warn("Current GPS is None. Cannot check checkpoint reached.")
            return
        
        # Calculate the distance to the current checkpoint
        current_gps_lat = np.radians(self.current_gps.latitude)
        current_gps_lon = np.radians(self.current_gps.longitude)
        checkpoint_lat = np.radians(self.checkpoints_list.poses[self.latest_scanned_checkpoint].pose.position.latitude)
        checkpoint_lon = np.radians(self.checkpoints_list.poses[self.latest_scanned_checkpoint].pose.position.longitude)
        # Calculate the distance to the checkpoint
        distance = haversine_distances(
            [[current_gps_lat, current_gps_lon], [checkpoint_lat, checkpoint_lon]]
        ) * 6371000
        distance = distance[0][1]
        self.get_logger().info(f"Distance to next checkpoint: {distance} m.")

        if distance < 15.0:
            start_time = time.time()
            checkpoint_reached_response = requests.post(f"{self.earthrover_sdk_url}/checkpoint-reached", json={})
            try:
                checkpoint_reached_response_json = checkpoint_reached_response.json()
            except Exception as e:
                self.get_logger().warn("Failed to parse checkpoint reached response.")
                checkpoint_reached_response_json = None

            if checkpoint_reached_response_json is not None:
                print(f"Checkpoint reached response: {checkpoint_reached_response_json}")
                print(f"Time taken to get checkpoint reached response: {time.time() - start_time}")

                if checkpoint_reached_response.status_code == 400:
                    if checkpoint_reached_response_json["detail"]["proximate_distance_to_checkpoint"] is None:
                        self.get_logger().info("Reached the last checkpoint.")
                        self.reached_last_checkpoint = True
                elif checkpoint_reached_response.status_code == 200:
                    if checkpoint_reached_response_json["next_checkpoint_sequence"] is None:
                        self.reached_last_checkpoint = True
                        self.latest_scanned_checkpoint = len(self.checkpoints_list.poses)-1
                    else:
                        self.latest_scanned_checkpoint = checkpoint_reached_response_json["next_checkpoint_sequence"]-1

        pose_list = self.checkpoints_list.poses[self.latest_scanned_checkpoint:]
        checkpoints_list = GeoPath()
        checkpoints_list.poses = pose_list
        print(f"Current target checkpoint: {self.latest_scanned_checkpoint+1}")

        self.waypoints_pub.publish(checkpoints_list)
            


def main(args=None):
    rclpy.init(args=args)
    executor = MultiThreadedExecutor(num_threads=8)
    waypoint_manager_node = WaypointReceiverNode()
    executor.add_node(waypoint_manager_node)
    executor.spin()
    executor.shutdown()
    waypoint_manager_node.destroy_node()
    rclpy.shutdown()

if __name__ == '__main__':
    main()
