#!/usr/bin/env python3
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Image
from sensor_msgs.msg import MagneticField
from sensor_msgs.msg import NavSatFix  # Import the GPS message type
from cv_bridge import CvBridge
from std_msgs.msg import Float32

import cv2
import numpy as np

from filterpy.kalman import KalmanFilter
from scipy.interpolate import UnivariateSpline
from earthrovers_state_estimation.kf_utils import kf_predict, kf_update

from collections import deque

class GPSYawEstimator:
    def __init__(self, window_size=15):
        self.gps_data = deque(maxlen=window_size)  # Store the last `window_size` GPS points

    def add_gps_point(self, lat, lon):
        """Add a new GPS point to the moving window."""
        self.gps_data.append((lat, lon))

    def calculate_yaw(self):
        """Calculate the absolute yaw heading based on the moving window of GPS data."""
        if len(self.gps_data) < 3:
            return None  # Not enough data to calculate yaw

        # Extract latitude and longitude
        lats, lons = zip(*self.gps_data)

        # Convert to radians
        lats = np.radians(lats)
        lons = np.radians(lons)
        
        # Ensure lons are strictly increasing
        sorted_indices = np.argsort(lons)
        lons = np.array(lons)[sorted_indices]
        lats = np.array(lats)[sorted_indices]

        # Fit a spline
        spline = UnivariateSpline(lons, lats, k=2, s=1)

        # Calculate the derivative (slope) at the last point
        slope = spline.derivative()(lons[-1])

        # Calculate the yaw
        yaw = np.arctan(slope)

        # Convert bearing from radians to degrees and normalize to [0, 360)
        #bearing = np.degrees(bearing)
        #bearing = (bearing + 360) % 360
        # avoid getting nan values
        return yaw

class YawEstimatorNode(Node):
    def __init__(self):
        super().__init__('yaw_estimator_node')
        self.subscription = self.create_subscription(
            Image,
            '/camera/front',
            self.listener_callback,
            10)
        
        self.subscription = self.create_subscription(
            Image,
            '/front_camera/image_raw',
            self.listener_callback,
            10)
        
        self.subscription = self.create_subscription(
            MagneticField,
            '/compass',
            self.compass_callback,
            10)
        
        self.orientation_subscription = self.create_subscription(
            Float32,
            '/orientation',
            self.orientation_callback,
            10)

        
        
        # GPS subscription
        self.gps_subscription = self.create_subscription(
            NavSatFix,
            '/gps',
            self.gps_callback,
            10
        )
        self.lat = None
        self.lon = None
        self.gps_yaw = None
        self.gps_yaw_estimator = GPSYawEstimator(window_size=5)
        self.gps_int_cnt = 0
        self.yaw_rate_pub = self.create_publisher(Float32, '/yaw_rate', 10)
        self.yaw_est_pub = self.create_publisher(Float32, '/orientation_filtered', 10)

        self.dx_pub = self.create_publisher(Float32, '/optical_flow_yaw/flow_avg_dx', 10)
        self.dy_pub = self.create_publisher(Float32, '/optical_flow_yaw/flow_avg_dy', 10)
        self.ori_pub = self.create_publisher(Float32, '/orientation_comp', 10)
        self.image_pub = self.create_publisher(Image, '/optical_flow_yaw/image_with_arrows', 10)
        self.last_stamp = None
        self.compass_yaw = None
        self.max_yaw_rate = 3/5*np.pi
        self.yaw_rate_correction = 1.5
        
        self.camera_count= 0
        
        self.t_opt = 0.0
        self.t_comp = 0.0
        self.yaw_est = 0.0
        self.prev_gray = None
        self.prev_features = None
        self.yaw_rate = np.zeros(2)
        self.bridge = CvBridge()
        self.last_time = self.get_clock().now()
        self.init_camera()
        self.get_logger().info(f"Node initialized: {self.get_name()}")
        
        ## Kalman filter
        
    
    def kf_init(self,compass_yaw):
        # self.kf = KalmanFilter(dim_x=2, dim_z=1)
        self.kf_dim_x = 2
        self.kf_dim_z = 1
        self.kf_x = np.array([[compass_yaw], [0.0]])     # [yaw, yaw_rate]
        self.kf_z = np.array([[compass_yaw]])
        self.kf_F = np.eye(self.kf_dim_x)
        self.kf_H = np.array([[1, 0]])           # We observe yaw (from compass)
        self.kf_P = np.eye(self.kf_dim_x)
        self.kf_P *= 10.0
        self.kf_R = np.array([[0.1]])            # Compass noise
        self.kf_Q = np.array([[0.0001, 0.0],  # Process noise
                            [0.0, 0.0001]])  # Ensure Q is 3x3
        self.kf_time = self.t_opt  # timestamp of last filter update
        self.get_logger().info(f"KF initialized: {self.get_name()}")
        
    def kf_reset(self,reset_x, reset_time, rate=0.0):
        self.get_logger().info(f"resetting Filter to {reset_x:.4f} at time {reset_time:.4f}")
        self.kf_x[0, 0] = reset_x
        self.kf_time = reset_time
        self.kf_x[1, 0] = rate
        
    def predict(self, yaw_rate):
        if self.kf_time is not None:
            dt = (self.t_opt - self.kf_time)
            self.kf_F = np.array([[1, dt],  # Yaw is updated by yaw_rate and bias
                                  [0, 1]])    # Yaw rate remains constant
            
            # propagate and Normalize yaw to [-π, π]
            self.kf_x, self.kf_P = kf_predict(self.kf_x, self.kf_P, self.kf_F, self.kf_Q)

            # Inject yaw_rate into the state velocity
            self.kf_x[1, 0] =  np.clip(yaw_rate, -self.max_yaw_rate, self.max_yaw_rate)

        self.kf_time = self.t_opt  # Update filter time after predict§
    
    def update(self, compass_yaw):
        if self.kf_time is not None:
            dt = (self.t_comp - self.kf_time)
            self.kf_F = np.array([[1, dt],
                                [0, 1]])
            
            # propagate and Normalize yaw to [-π, π] before the update
            self.kf_x, self.kf_P = kf_predict(self.kf_x, self.kf_P, self.kf_F, self.kf_Q)
            
            self.kf_z = np.array([[compass_yaw]])
            self.kf_x, self.kf_P = kf_update(self.kf_x, self.kf_P, self.kf_z, self.kf_R, self.kf_H)
            
            self.kf_time = self.t_comp
            
    
            
    def get_kf_output(self):
        return self.kf_x[0, 0]
    
    def gps_callback(self, msg):
        """Callback for GPS data."""
        self.lat = msg.latitude
        self.lon = msg.longitude
        self.gps_yaw_estimator.add_gps_point(self.lat, self.lon)
        #self.get_logger().info(f"Received GPS data: lat={self.lat}, lon={self.lon}")
        
        if self.gps_int_cnt < 15:
            self.gps_int_cnt += 1
        else:
            # Calculate yaw
            gps_yaw = self.gps_yaw_estimator.calculate_yaw() 
            if gps_yaw is not None and not np.isnan(gps_yaw):
                self.gps_yaw = gps_yaw
        
    def init_camera(self):
        # 你的相机内参
        self.fx = 407.86023253
        self.fy = 407.86605705
        self.cx = 533.30109486
        self.cy = 278.69939958
        self.K = np.array([[407.86023253, 0., 533.30109486],
                          [0., 407.86605705, 278.69939958],
                          [0., 0., 1.]])

        # 你的畸变系数
        self.D = np.array([-0.2171785, 0.05372816, 0.00185307, -0.0021051, -0.00599918])
        self.R = np.array([
            [1, 0, 0],
            [0, 1, 0],
            [0, 0, 1]
        ])
        #self.D = np.array([-0.792, -0.597, -0.184, 0, 0], dtype=np.float32)  # [k1, k2, p1, p2, k3]
        self.image_size = (1024, 576)
        self.fov = 2* np.arctan2(self.image_size[0],(self.fx*2))
        print(f"FOV: {self.fov:.4f} rad")
        # Compute undistortion maps once
        self.map1, self.map2 = cv2.initUndistortRectifyMap(
            self.K, self.D, self.R, self.K, self.image_size, cv2.CV_16SC2)
        
    def compass_callback(self, msg):
        if self.compass_yaw is None:
            self.compass_yaw = self.yaw_from_mag(msg)
            self.kf_init(self.compass_yaw)
        else:
            self.compass_yaw = self.yaw_from_mag(msg)
            
        if self.compass_yaw is None:
            self.kf_init(self.compass_yaw)
        
        yaw = np.degrees(self.compass_yaw) 
        if yaw < 0:
            yaw += 360.0
        self.ori_pub.publish(Float32(data=float(yaw)))
        
        self.t_comp = self.stamp_to_time(msg.header.stamp)
        
        if self.camera_count < 1:
            self.get_logger().info(f"loss of camera frames, skipping update")
            self.kf_reset(self.compass_yaw, self.t_comp)
            return
        self.update(self.compass_yaw)
        self.camera_count = 0
        #print(f"Yaw from magnetometer: {self.compass_yaw:.4f} rad")
        
    def orientation_callback(self, msg):
        compass_yaw = np.radians(msg.data)
        # Convert to radians in [-pi, pi]
        if compass_yaw > np.pi:
            compass_yaw -= 2 * np.pi
        
        if self.compass_yaw is None:
            self.kf_init(compass_yaw)
            
        
        
        self.compass_yaw = compass_yaw
        self.t_comp = self.t_opt #self.get_clock().now().nanoseconds * 1e-9
        
        if self.camera_count < 1:
            self.get_logger().info(f"loss of camera frames, skipping update")
            self.kf_reset(self.compass_yaw, self.t_comp)
            return

        #self.get_logger().info(f"SDK orientation: {self.compass_yaw:.4f} rad")
        self.update(self.compass_yaw)
        self.camera_count = 0
        
    def stamp_to_time(self, stamp):
        return (stamp.sec + stamp.nanosec * 1e-9)

    def listener_callback(self, msg):
        self.camera_count += 1
        cv_image = self.bridge.imgmsg_to_cv2(msg, desired_encoding='bgr8')
        h,  w = cv_image.shape[:2]
        #newcameramtx, roi = cv2.getOptimalNewCameraMatrix(self.K, self.D, (w,h), 1, (w,h))
        cv_image =  cv2.remap(cv_image, self.map1, self.map2, interpolation=cv2.INTER_LINEAR) #cv2.undistort(cv_image, self.K, self.D)# cv_image =  cv2.remap(cv_image, self.map1, self.map2, interpolation=cv2.INTER_LINEAR) #cv2.undistort(cv_image, self.K, self.D, None, newcameramtx)# 
        
        gray = cv2.cvtColor(cv_image, cv2.COLOR_BGR2GRAY)
        
        current_stamp = msg.header.stamp
        self.t_opt = self.stamp_to_time(current_stamp)
        if self.last_stamp is not None:
            dt = self.stamp_to_time(current_stamp) - self.stamp_to_time(self.last_stamp)
        else:
            dt = 0.0  # First frame
        self.last_stamp = current_stamp

        if self.prev_gray is not None:
            if self.prev_features is None or len(self.prev_features) == 0:
                self.prev_features = cv2.goodFeaturesToTrack(self.prev_gray, 100, 0.3, 7)

            if self.prev_features is not None:
                try:
                    next_features, status, _ = cv2.calcOpticalFlowPyrLK(
                        self.prev_gray, gray, self.prev_features, None)
                    if next_features is not None:
                        dx = 0.0
                        dy = 0.0
                        count = 0
                        for i, st in enumerate(status):
                            if st == 1:
                                dx += (next_features[i][0][0] - self.prev_features[i][0][0])
                                dy += (next_features[i][0][1] - self.prev_features[i][0][1])

                                count += 1

                        if count > 0:
                            avg_dx = dx / count
                            avg_dy = dy / count
                            flow_mag_dx = abs(avg_dx)
                            flow_mag_dy = abs(avg_dy)
                            self.dx_pub.publish(Float32(data=float(flow_mag_dx)))
                            self.dy_pub.publish(Float32(data=float(flow_mag_dy)))

                            if dt > 0 and self.compass_yaw is not None:
                                # Calculate yaw rate
                                self.yaw_rate = - avg_dx/self.image_size[0] * self.fov / dt * self.yaw_rate_correction
                                self.tilt_rate = - avg_dy/self.image_size[1] * self.fov / dt * self.yaw_rate_correction
                                # publish yaw rate
                                self.yaw_rate_pub.publish(Float32(data=float(np.clip(self.yaw_rate, -self.max_yaw_rate, self.max_yaw_rate))))
                                self.predict(self.yaw_rate)
                                self.yaw_est = self.get_kf_output()
                                yaw_est_deg = np.degrees(self.yaw_est) 
                                if yaw_est_deg < 0:
                                    yaw_est_deg += 360.0
                                self.yaw_est_pub.publish(Float32(data=float(yaw_est_deg)))
                                if self.compass_yaw is not None and self.gps_yaw is not None:
                                    self.get_logger().info(f"GPS Yaw: {self.gps_yaw:.4f} , Compass Yaw: {self.compass_yaw:.4f} , Yaw Est: {self.yaw_est:.4f}") 
                                    
                                elif self.compass_yaw is not None:
                                    self.get_logger().info(f"Compass Yaw: {self.compass_yaw:.4f} , Yaw Est: {self.yaw_est:.4f} ") 
                
                except cv2.error as e:
                    self.get_logger().warn(f"Optical flow failed: {str(e)}")  
        #print(self.compass_yaw)
        #if self.compass_yaw is not None : self.draw_arrows(cv_image)

        # Prepare for next frame

        self.prev_gray = gray
        self.prev_features = cv2.goodFeaturesToTrack(self.prev_gray, 100, 0.3, 7)
        
    def draw_arrows(self, cv_image):
        scaling_factor = 0.1
        # Draw arrows
        img_with_arrows = cv_image.copy()
        h, w = img_with_arrows.shape[:2]
        center = (w // 2, h // 2)
        scale = 500*scaling_factor  # Adjust this scale based on desired arrow length

        end_x = int(center[0] + self.yaw_rate * scale)
        end_y = int(center[1] + self.tilt_rate * scale)

        # Horizontal arrow (red)
        cv2.arrowedLine(img_with_arrows, center, (end_x, center[1]), (0, 0, 255), 2, tipLength=0.3)
        # Vertical arrow (blue)
        cv2.arrowedLine(img_with_arrows, center, (center[0], end_y), (255, 0, 0), 2, tipLength=0.3)
        
        # --- Draw transparent dark circle and compass arrow ---
        overlay = img_with_arrows.copy()
        output = img_with_arrows.copy()

        circle_center = (img_with_arrows.shape[1] - 60, 60)  # top-right corner
        circle_radius = 30
        alpha = 0.5  # Transparency factor

        # Draw dark circle on overlay
        cv2.circle(overlay, circle_center, circle_radius, (30, 30, 30), -1)  # dark gray fill

        # Blend overlay with original image
        cv2.addWeighted(overlay, alpha, output, 1 - alpha, 0, output)
        
        # Draw compass arrow on top of the blended image
        arrow_length = 25
        dx = int(arrow_length * np.sin(self.compass_yaw))
        dy = int(-arrow_length * np.cos(self.compass_yaw))  # Y is inverted in image space

        arrow_tip = (circle_center[0] + dx, circle_center[1] + dy)
        cv2.arrowedLine(output, circle_center, arrow_tip, (0, 255, 0), 2, tipLength=0.3)
        
        # Draw compass arrow on top of the blended image <-- Yaw Estimation
        arrow_length = 25
        dx = int(arrow_length * np.sin(self.yaw_est))
        dy = int(-arrow_length * np.cos(self.yaw_est))  # Y is inverted in image space

        arrow_tip_est = (circle_center[0] + dx, circle_center[1] + dy)
        cv2.arrowedLine(output, circle_center, arrow_tip_est, (0, 0, 255), 2, tipLength=0.3)
        
        if self.gps_yaw is not None and False:
            # Draw compass arrow on top of the blended image <-- GPS Yaw Estimation
            arrow_length = 25
            dx = int(arrow_length * np.sin(self.gps_yaw))
            dy = int(-arrow_length * np.cos(self.gps_yaw))  # Y is inverted in image space

            arrow_tip_gps = (circle_center[0] + dx, circle_center[1] + dy)
            cv2.arrowedLine(output, circle_center, arrow_tip_gps, (255, 0, 0), 2, tipLength=0.3)

        # Label 'N' for North
        cv2.putText(output, "N", (circle_center[0] - 7, circle_center[1] - circle_radius - 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 255, 200), 1)

        # Use this final image for publishing
        img_with_arrows = output

        # Publish debug image
        debug_msg = self.bridge.cv2_to_imgmsg(img_with_arrows, encoding='bgr8')
        self.image_pub.publish(debug_msg)
        
    def yaw_from_mag(self, msg):
        # Switch from DES to NED coordinate system.
        x, y, z = -msg.magnetic_field.z, msg.magnetic_field.y, -msg.magnetic_field.x

        # LSB Raw data to Gauss
        x /= 3000
        y /= 3000

        # Calculate the yaw
        yaw = np.arctan2(y, x)

        # if correct_declination:
        #     first_gps = gps_data.loc[gps_data.first_valid_index()]
        #     declination = get_magnetic_declination(
        #         lat=first_gps['latitude'],
        #         lon=first_gps['longitude'],
        #         date=gps_data.first_valid_index(),
        #     )
        #     yaw += declination
        return yaw

def main(args=None):
    rclpy.init(args=args)
    node = YawEstimatorNode()
    rclpy.spin(node)
    print(node.kf_P)
    node.destroy_node()
    rclpy.shutdown()
    
if __name__ == "__main__":
    main()
    
    

