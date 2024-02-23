import time
from collections import deque
from pathlib import Path
import multiprocessing as mp
from multiprocessing import shared_memory

import numpy as np
import open3d as o3d
import pinocchio as pin
from scipy.spatial.transform import Rotation

from deoxys.franka_interface import FrankaInterface
from deoxys.utils.transform_utils import quat2axisangle, quat2mat
from deoxys.utils import YamlConfig
from rpal.algorithms.grid import SurfaceGridMap
from rpal.algorithms.search import (
    RandomSearch,
    SearchHistory,
    Search,
    ActiveSearch,
    ActiveSearchWithRandomInit,
    ActiveSearchAlgos,
)
from rpal.utils.control_utils import generate_joint_space_min_jerk
from rpal.utils.data_utils import DatasetWriter
from rpal.utils.devices import ForceSensor
from rpal.utils.interpolator import Interpolator, InterpType
from rpal.utils.time_utils import Ratekeeper
from rpal.utils.proc_utils import RingBuffer, RunningStats
import rpal.utils.constants as rpal_const
from rpal.utils.constants import PALP_CONST
from rpal.utils.constants import PalpateState
from rpal.utils.pcd_utils import scan2mesh, mesh2roi
from tfvis.visualizer import RealtimeVisualizer


def main_ctrl(shm_buffer, stop_event: mp.Event, save_folder: Path, search: Search):
    existing_shm = shared_memory.SharedMemory(name=shm_buffer)
    data_buffer = np.ndarray(1, dtype=rpal_const.PALP_DTYPE, buffer=existing_shm.buf)
    np.random.seed(PALP_CONST.seed)
    goals = deque([])
    palp_state = PalpateState()
    curr_pose_se3 = pin.SE3.Identity()
    using_force_control_flag = False
    collect_points_flag = False
    force_buffer = RingBuffer(PALP_CONST.buffer_size)
    pos_buffer = RingBuffer(PALP_CONST.buffer_size)
    running_stats = RunningStats()
    curr_eef_pose = None
    F_norm = 0.0
    Fxyz = np.zeros(3)
    palp_pt = None
    surf_normal = None
    palp_progress = 0.0  # [0, 1]
    palp_id = 0
    stiffness = 0.0
    search_history = SearchHistory()

    oscill_start_time = None
    start_angles = np.full(2, -PALP_CONST.angle_oscill)  # theta, phi
    end_angles = np.full(2, PALP_CONST.angle_oscill)
    force_oscill_out = generate_joint_space_min_jerk(
        start_angles,
        end_angles,
        PALP_CONST.t_oscill / 2,
        1 / PALP_CONST.ctrl_freq,
    )
    force_oscill_in = generate_joint_space_min_jerk(
        end_angles,
        start_angles,
        PALP_CONST.t_oscill / 2,
        1 / PALP_CONST.ctrl_freq,
    )
    force_oscill_traj = force_oscill_out + force_oscill_in

    force_cap = ForceSensor()
    robot_interface = FrankaInterface(
        str(rpal_const.PAN_PAN_FORCE_CFG),
        use_visualizer=False,
        control_freq=80,
    )
    force_ctrl_cfg = YamlConfig(str(rpal_const.FORCE_CTRL_CFG)).as_easydict()
    osc_abs_ctrl_cfg = YamlConfig(str(rpal_const.OSC_ABSOLUTE_CFG)).as_easydict()
    robot_interface._state_buffer = []
    interp = Interpolator(interp_type=InterpType.SE3)

    # search = ActiveSearch(phantom_pcd, ActiveSearchAlgos.BO)

    def palpate(pos, O_surf_norm_unit=np.array([0, 0, 1])):
        assert np.isclose(np.linalg.norm(O_surf_norm_unit), 1)

        # Uses Kabasch algo get rotation that aligns the eef tip -z_axis and
        # the normal vector, and secondly ensure that the y_axis is aligned
        R = Rotation.align_vectors(
            np.array([O_surf_norm_unit, np.array([0, -1, 0])]),
            np.array([[0, 0, -1], np.array([0, 1, 0])]),
            weights=np.array([10, 0.1]),
        )[0].as_matrix()

        palp_se3 = pin.SE3.Identity()
        palp_se3.translation = pos - PALP_CONST.palpate_depth * O_surf_norm_unit
        palp_se3.rotation = R

        above_se3 = pin.SE3.Identity()
        above_se3.translation = pos + PALP_CONST.above_height * O_surf_norm_unit
        above_se3.rotation = R

        reset_pose = pin.SE3.Identity()
        reset_pose.translation = rpal_const.RESET_PALP_POSE[:3]
        reset_pose.rotation = quat2mat(rpal_const.RESET_PALP_POSE[3:7])

        goals.appendleft(above_se3)
        goals.appendleft(palp_se3)
        goals.appendleft(reset_pose)

    def state_transition():
        palp_state.next()
        steps = rpal_const.STEP_FAST
        if palp_state.state == PalpateState.PALPATE:
            steps = rpal_const.STEP_SLOW
        pose_goal = goals.pop()
        interp.init(curr_pose_se3, pose_goal, steps=steps)

    while len(robot_interface._state_buffer) == 0:
        continue

    start_pose = pin.SE3.Identity()
    start_pose.translation = rpal_const.GT_SCAN_POSE[:3]
    start_pose.rotation = quat2mat(rpal_const.GT_SCAN_POSE[3:7])
    goals.appendleft(start_pose)

    curr_eef_pose = robot_interface.last_eef_rot_and_pos
    curr_pose_se3.rotation = curr_eef_pose[0]
    curr_pose_se3.translation = curr_eef_pose[1]
    pose_goal = goals.pop()
    interp.init(curr_pose_se3, pose_goal, steps=rpal_const.STEP_FAST)
    try:
        while not stop_event.is_set():
            curr_eef_pose = robot_interface.last_eef_rot_and_pos
            curr_pose_se3.rotation = curr_eef_pose[0]
            curr_pose_se3.translation = curr_eef_pose[1]

            Fxyz_temp = force_cap.read()
            if Fxyz_temp is not None:
                Fxyz = Fxyz_temp
                F_norm = np.sqrt(np.sum(Fxyz**2))

            force_buffer.append(F_norm)
            if palp_pt is not None:
                pos_buffer.append(np.linalg.norm(palp_pt - curr_pose_se3.translation))

            if force_buffer.overflowed():
                running_stats.update(force_buffer.buffer)

            # done with palpation
            if (
                using_force_control_flag
                and palp_progress >= 1
                # and pos_buffer.std < PALP_CONST.pos_stable_thres
            ):
                print("palpation done")
                collect_points_flag = False
                using_force_control_flag = False
                state_transition()
                palp_id += 1

            # initiate palpate
            if len(goals) > 0 and interp.done:
                state_transition()
            elif len(goals) == 0 and interp.done:
                palp_pt, surf_normal = search.next()
                search_history.add(*search.grid_estimate)
                palpate(palp_pt, surf_normal)

            # start palpation
            stiffness = 0.0
            if palp_state.state == PalpateState.PALPATE:
                assert palp_pt is not None

                # update stiffness
                palp_disp = curr_pose_se3.translation - palp_pt

                # scalar projection of displacement along -surface normal normalized to max alotted displacement
                # between 0 and 1
                palp_progress = (
                    np.dot(palp_disp, -surf_normal) / PALP_CONST.max_palp_disp
                )
                if (
                    Fxyz[2] >= PALP_CONST.max_Fz or palp_progress >= 1.0
                ) and not using_force_control_flag:
                    print("CONTOUR FOLLOWING!")
                    stiffness = Fxyz[2] / (
                        np.linalg.norm(palp_pt - curr_pose_se3.translation) + 1e-6
                    )
                    stiffness /= PALP_CONST.stiffness_normalization
                    using_force_control_flag = True
                    collect_points_flag = True
                    oscill_start_time = time.time()
                    print("STIFFNESS: ", stiffness)

                    search.update_outcome(
                        stiffness
                        if stiffness >= PALP_CONST.stiffness_tumor_filter
                        else 0.0
                    )

            # control: force
            if using_force_control_flag:
                if time.time() - oscill_start_time > PALP_CONST.t_oscill:
                    oscill_start_time = time.time()
                idx = int(
                    (time.time() - oscill_start_time) / (1 / PALP_CONST.ctrl_freq)
                )
                action = np.zeros(9)
                oscill_pos = force_oscill_traj[idx]["position"]
                action[0] = oscill_pos[0]
                action[1] = oscill_pos[1]
                action[2] = -0.005
                robot_interface.control(
                    controller_type=rpal_const.FORCE_CTRL_TYPE,
                    action=action,
                    controller_cfg=force_ctrl_cfg,
                )

            # control: OSC
            else:
                action = np.zeros(7)
                next_se3_pose = interp.next()
                target_xyz_quat = pin.SE3ToXYZQUAT(next_se3_pose)
                axis_angle = quat2axisangle(target_xyz_quat[3:7])

                # print(se3_pose)
                action[:3] = target_xyz_quat[:3]
                action[3:6] = axis_angle
                # print(action)

                robot_interface.control(
                    controller_type=rpal_const.OSC_CTRL_TYPE,
                    action=action,
                    controller_cfg=osc_abs_ctrl_cfg,
                )
            q, p = robot_interface.last_eef_quat_and_pos
            save_action = np.zeros(9)
            save_action[: len(action)] = action
            data_buffer[0] = (
                Fxyz,
                q.flatten(),  # quat
                p.flatten(),  # pos
                target_xyz_quat[3:7],
                target_xyz_quat[:3],
                save_action,
                palp_progress,
                palp_pt if palp_pt is not None else np.zeros(3),
                surf_normal if surf_normal is not None else np.zeros(3),
                palp_id,
                palp_state.state,
                stiffness,
                using_force_control_flag,
                collect_points_flag,
            )

    except KeyboardInterrupt:
        pass

    # stop
    robot_interface.close()
    search_history.save(save_folder)
    print("history saved")
    existing_shm.close()


if __name__ == "__main__":

    pcd = o3d.io.read_point_cloud(str(rpal_const.SURFACE_SCAN_PATH))
    surface_mesh = scan2mesh(pcd)
    roi_pcd = mesh2roi(surface_mesh, bbox_pts=rpal_const.BBOX_DOCTOR_ROI)
    # roi_pcd = mesh2roi(surface_mesh)
    surface_grid_map = SurfaceGridMap(
        roi_pcd, grid_size=rpal_const.PALP_CONST.grid_size
    )
    search = ActiveSearchWithRandomInit(
        ActiveSearchAlgos.BO,
        surface_grid_map,
        kernel_scale=rpal_const.PALP_CONST.kernel_scale,
        random_sample_count=rpal_const.PALP_CONST.random_sample_count,
    )
    search = RandomSearch(surface_grid_map)
    # search.grid.visualize()
    dataset_writer = DatasetWriter(print_hz=False)

    data_buffer = np.zeros(1, dtype=rpal_const.PALP_DTYPE)
    shm = shared_memory.SharedMemory(create=True, size=data_buffer.nbytes)
    data_buffer = np.ndarray(data_buffer.shape, dtype=data_buffer.dtype, buffer=shm.buf)
    stop_event = mp.Event()
    ctrl_process = mp.Process(
        target=main_ctrl,
        args=(shm.name, stop_event, dataset_writer.dataset_folder, search),
    )
    ctrl_process.start()

    subsurface_pts = []

    rk = Ratekeeper(50, name="data_collect")

    rtv = RealtimeVisualizer()
    rtv.add_frame("BASE")
    rtv.set_frame_tf("BASE", np.eye(4))
    rtv.add_frame("EEF", "BASE")
    try:
        while True:
            if np.all(data_buffer["O_q_EE"] == 0):
                # print("Waiting for deoxys...")
                continue

            O_p_EE = data_buffer["O_p_EE"].flatten()
            O_p_EE_target = data_buffer["O_p_EE_target"].flatten()
            O_q_EE_target = data_buffer["O_q_EE_target"].flatten()
            O_q_EE = data_buffer["O_q_EE"].flatten()
            if data_buffer["collect_points_flag"]:
                subsurface_pts.append(O_p_EE)

            print(data_buffer)
            # print("Pos ERROR: ", np.linalg.norm(O_p_EE - O_p_EE_target))
            # print("Rot ERROR: ", np.linalg.norm(O_q_EE - O_q_EE_target))

            dataset_writer.add_sample(data_buffer.copy())
            ee_pos = np.array(O_p_EE)
            ee_rmat = quat2mat(O_q_EE)
            O_T_E = np.eye(4)
            O_T_E[:3, :3] = ee_rmat
            O_T_E[:3, 3] = ee_pos
            rtv.set_frame_tf("EEF", O_T_E)
            rk.keep_time()
    except KeyboardInterrupt:
        pass
    stop_event.set()
    print("CTRL STOPPED!")

    while ctrl_process.is_alive():
        continue
    ctrl_process.join()

    dataset_writer.save_subsurface_pcd(np.array(subsurface_pts).squeeze())
    dataset_writer.save_roi_pcd(roi_pcd)
    dataset_writer.save_grid_pcd(surface_grid_map.grid_pcd)
    dataset_writer.save()
    shm.close()
    shm.unlink()
