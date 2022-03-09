import inspect
import logging
import multiprocessing
import random as rnd
import sys

import numpy as np
from numpy.random import default_rng
import line_profiler

from libraries.ray_traversal import traversal


class ETrajMap3d(object):
    """
    Implements energy deposition and surface secondary electron flux calculation.
    """
    def __init__(self, deposit, surface, sim, segment_min_length =0.3):
        #TODO: ETrajMap3d class should probably be merged with Etrajectory
        # These classes
        self.grid = deposit
        self.state = deposit
        self.surface = surface
        self.cell_dim = sim.cell_dim # absolute dimension of a cell, nm
        self.nz, self.ny, self.nx = np.asarray(self.grid.shape)- 1 # simulation chamber dimensions
        self.zdim_abs, self.ydim_abs, self.xdim_abs = [x*self.cell_dim for x in self.grid.shape]
        self.DE = np.zeros((self.nz+1, self.ny+1, self.nx+1)) # array for storing of deposited energies
        self.flux = np.zeros((self.nz+1, self.ny+1, self.nx+1)) # array for storing SE fluxes
        self.amplifying_factor = 10000 # artificially increases SE yield to preserve accuracy
        # self.e = e # fitting parameter related to energy required to initiate a SE cascade, material specific, eV
        self.deponat = sim.deponat
        self.substrate = sim.substrate
        # self.lambda_escape = lambda_escape # mean free escape path, material specific, nm
        # self.dn = floor(self.lambda_escape * 2 / self.cell_dim) # number of cells an SE can intersect
        self.trajectories = [] # holds all trajectories mapped to 3d structure
        self.se_traj = []
        self.x0, self.y0, self.z0 = 0, 0, 0  # origin of 3d grid
        rnd.seed()
        self.segment_min_length = segment_min_length
        self.m3d = None

    # @nb.jit(nopython=True)
    def __arr_min(self, x):
        if x[0] >= x[1]:
            if x[1] >= x[2]:
                return x[2], 2
            else:
                return x[1], 1
        else:
            if x[0] >= x[2]:
                return x[2], 2
            else:
                return x[0], 0

    # @nb.jit(nopython=True)
    def traverse_cells(self, p0, pn, direction, t, step_t):
        """
            AABB Ray-Voxel traversal algorithm.
            Gets coordinates, where ray crosses voxel walls

        :param p0: ray origin
        :param pn: ray endpoint
        :param direction: direction of the ray
        :param t: first t-value
        :param step_t: step of the t-value
        :return:
        """

        crossings = [p0]  # first point is always ray origin
        while True:  # iterating until the end of the ray
            next_t, ind = self.__arr_min(t)  # minimal t-value corresponds to the box wall crossed; 2x faster than t.min() !!
            if next_t > 1:  # finish if trajectory ends inside a cell (t>1)
                crossings.append(pn)
                break
            crossing_point = p0 + next_t * direction  # actual crossing point
            # if crossing_point[0] >= self.zdim_abs or crossing_point[1] >= self.ydim_abs or crossing_point[2] >= self.xdim_abs:  # 5x faster than ().any() !!!!!
            #     break
            t[ind] += step_t[ind]  # going to the next wall; 7x faster than with (t==next_t)
            crossings.append(crossing_point)  # saving crossing point
        return crossings

    def follow_segment(self, points, dEs):
        """
        Calculates distances traversed by a trajectory segment inside cells
        and gets energy losses corresponding to the distances.

        :param points: array of (z, y, x) points representing a trajectory from MC simulation
        :param dEs:  list of energies losses between consecutive points. dEs[0] corresponds to a loss between p[0] and p[1]
        :return:
        """

        # Cell traversal algorithm taken from https://www.shadertoy.com/view/XddcWn#
        # Electron trajectory segments are treated as rays

        # Algorithm is vectorized, thus following calculations are done for all segments in a trajectory
        p0, pn = points[:, 0], points[:, 1]  # ray origin and end
        direction = points[:, 1, :] - points[:, 0, :] # vector of a ray

        # L = np.linalg.norm(direction, axis=1) # length of a rays
        L = np.empty_like(dEs)
        traversal.det_2d(direction, L)
        des = dEs/L
        sign = np.int8(np.sign(direction))
        step = sign * self.cell_dim # distance traveled by a ray along each axis in the ray direction, when crossing a cell
        # step_t = step / direction  # iteration step of the t-values
        step_t = None
        # Catching 'Division by zero' error
        try:
            with np.errstate(divide='raise', invalid='raise'):
                step_t = step / direction # iteration step of the t-values
        except Exception as e:
            print(e.args)
            zeros = np.nonzero(direction==0)[0]
            for i in range(len(zeros)):
                print(f'p0: {p0[zeros[i]]}, pn: {pn[zeros[i]]}, direction: {direction[zeros[i]]}, L: {L[zeros[i]]}')
            step_t = step / direction

        delta = -(points[:, 0] % self.cell_dim) # positions of the ray origin relative to its enclosing cell position
        t = np.abs((delta + (step == self.cell_dim) * self.cell_dim + (delta == 0) * step) / direction) # initial t-value
        max_traversed_cells = int(L.max()/self.cell_dim*2)+10 # maximum number of cells traversed by a segment in the trajectory;
        # this is essential to allocate enough memory for the traversal algorithm
        print(f'*Running \'traverse_segment\'...', end='')
        traversal.traverse_segment(self.DE, self.grid, self.cell_dim, p0, pn, direction, t, step_t, des, max_traversed_cells)
        print('finished.')

    def prep_se_emission(self, points, dEs, ends):
        """
        The idea behind this method is to divide trajectory segments into pieces
        and emit SEs from every piece based on the energy lost on it.

        :param passes:
        :return:
        """

        # Segments are divided into even parts, that become SE emission centers.
        # It has been observed, that segments are often smaller than the cell (~0.5nm average)
        # Thus, firstly, short segments are separated from longer ones and are left untouched.
        # Long segments are divided into even parts based on the 'segment_min_length' attribute of the ETrajMap3d class
        # All SE segments or 'vectors' are collected in a single array
        # NOTE: because SE 'vectors' are 'emitted' from the first point of the segment, the last point
        #  at the exit point has no emitted SE. This may significantly reduce SE surface yield at the exit points
        #  in grids with bigger cell size.
        #  A one additional emitted SE vector is added at the end of each trajectory in order to mitigate the problem.
        #  It is assigned the vector and energy of the last vector in the list.
        #  While major PEs do not re-enter solid on a simple structure, they might re-enter in more complex structures,
        #  that are not effectively taken into account.
        energies_all = []
        coords_all = []
        L = np.empty(dEs.shape, dtype=np.float64)
        traversal.det_2d(points[:, 1, :] - points[:, 0, :], L)
        # Collecting short segments that have energy loss
        short = (L <= self.segment_min_length).nonzero()[0]
        if short.shape[0]>0:
            coords_all.append(points[short,0].reshape(short.shape[0], 3))
            energies_all.append(dEs[short])

        # Collecting long segments
        long = (L > self.segment_min_length).nonzero()[0]
        if long.shape[0]>0:
            coords_long = np.take(points, long, axis=0)
            vector = coords_long[:, 1, :] - coords_long[:, 0, :]
            # BUG: np.ceil refuses to cast to integer even with 'casting=unsafe'
            num = np.intc(np.ceil(L[long] / self.segment_min_length)) # np.ceil r
            # delta = vector / np.broadcast_to(num, (3, num.shape[0])).T
            delta = vector/num.reshape(num.shape[0],1)
            N = num.sum(dtype=int)
            pieces = np.empty((N, 3))
            energies = np.empty(N)
            print('**Running \'divide_segments\'...', end='')
            traversal.divide_segments(dEs[long], coords_long[:,0], num, delta, pieces, energies) # Cython script
            print('finished.')
            if pieces.min() < 0:
                print(f'Encountered negative values in coordinates in prep_se_emission')
                print(f'Num: {num}, delta: {delta}, ')
                err_index = (pieces<0).nonzero()[0]
                print(f'Pieces:')
                print(*pieces[err_index], sep='\n\t')
                print('Energies:')
                print(*energies[err_index], sep='\n\t')
                print(f'Segments\' energies: {dEs[long]}')
                print(f'Segments\' coordinates: {coords_long[:,0]}')
                pieces[err_index] = np.fabs(pieces[err_index])
                frame = inspect.currentframe().f_back.f_back
                sim = frame.f_locals['sim']
                import os
                directory = os.path.dirname(os.path.realpath(__file__))
                filename = os.path.join(directory, '../passes_last.txt')
                sim.save_passes(filename, 'text')
            coords_all.append(pieces)
            energies_all.append(energies)
        coords_all.append(points[points.shape[0]-1,1].reshape(1,3))
        l = len(energies_all)-1
        energies_all.append(energies_all[l][energies_all[l].shape[0]-1].reshape(1))
        traj_ends = points[ends, 1,:]
        energies_ens = self.segment_min_length / L[ends] * dEs[ends]
        coords_all.append(traj_ends)
        energies_all.append(energies_ens)
        # Combining all the collected segments into one array
        coords_all = np.concatenate((coords_all), axis=0)
        if coords_all.min() < 0:
            print(f'Encountered negative values in coordinates in prep_se_emission')
            print(f'')
        energies_all = np.concatenate((energies_all), axis=0)
        self.dEs_all = energies_all
        self.coords_all = coords_all

    def generate_se(self):
        """
        Generate a random vector for every coordinate, calculate SEs emitted per each vector
        and collect them when they cross surface

        :return:
        """
        L = np.empty_like(self.dEs_all)
        direction = np.random.normal(0, 10, (self.dEs_all.shape[0], 3)) # creates spherically randomly distributed vectors
        traversal.det_2d(direction, L)
        direction /= L.reshape(L.shape[0],1) # normalizing
        sign = np.int8(np.sign(direction))
        sign[sign==-1] = 0
        sign[sign==1] = -1
        delta = -(self.coords_all % self.cell_dim)
        sign = (delta==0) * sign

        coords = (np.int64(self.coords_all / self.cell_dim)).T
        cell_material = self.grid[coords[0], coords[1], coords[2]]
        e = np.empty_like(cell_material)
        e[cell_material==-1] = self.deponat.e
        e[cell_material==-2] = self.substrate.e
        e[cell_material>=0] = 1000000
        lambda_escape = np.where(cell_material == -1, self.deponat.lambda_escape * 2, 0.00001) + np.where(cell_material == -2, self.substrate.lambda_escape * 2, 0.00001)
        n_se = self.dEs_all / e * self.amplifying_factor  # number of generated SEs, usually ~0.1

        length = lambda_escape # explicitly says that every vector has same length that equals SE escape path
        direction[:,0] *= length
        direction[:,1] *= length
        direction[:,2] *= length
        pn = direction + self.coords_all
        step = np.sign(direction) * self.cell_dim
        step_t = step / direction

        t = np.abs((delta + np.maximum(step, 0) + (delta == 0) * step) / direction)
        max_traversed_cells = int(np.amax(length, initial=0)/self.cell_dim*2+5)
        print('***Running \'generate_flux\'...', end='')
        # Here each vector is processed with the same ray traversal algorithm as in 'follow_segment' method.
        # It checks if vectors cross surface cells and if they do, the SE number associated with the vector
        # is collected in the cell crossed.
        traversal.generate_flux(self.flux, self.surface.view(dtype=np.uint8), self.cell_dim, self.coords_all, pn, direction, sign, t, step_t, n_se, max_traversed_cells) # Cython script
        print('finished.')
        self.coords_all = np.hstack((self.coords_all.reshape((pn.shape[0],1,3)), pn.reshape((pn.shape[0],1,3))))

    def __setup_trajectory(self, points, energies, mask):
        """
        Setup trajectory from MC simulation data for further computation.
        points: list of (x, y, z) points of trajectory from MC simulation
        energies: list of residual energies of electron at points of trajectory in keV
        Returns arrays of points and energy losses (in eV)
        """
        # Trajectories are divided into segments represented by a pair of points
        # Then mask is applied, selecting only segments that traverse solid
        # This reduces the unnecessary analysis of trajectory segments that lie in void(geometry features or backscattered electrons)
        mask = np.asarray(mask, dtype=bool)
        pnp = np.array(points[0:len(points)]) # to get easy access to x, y, z coordinates of points
        pairs = np.empty((pnp.shape[0]-1, 2, 3))
        pairs[:,0,:] = pnp[:-1]
        pairs[:,1,:] = pnp[1:]
        pairs = pairs[mask]
        # Workaround against duplicate points
        p0 = pairs[:,0,:]
        pn = pairs[:,1,:]
        pn[pn==p0] += rnd.choice((0.000001, -0.000001)) # protection against duplicate coordinates
        # p0, pn = pnp[:-1], pnp[1:]
        # pairs = np.stack((p0,pn))
        # TODO: Thrown 'axis don't match array' exception :
        # pairs = np.transpose(pairs, axes=(1,0,2))[mask.nonzero()]
        # np.delete(pairs, (mask==0), axis=0)
        # result = pairs[mask.nonzero()]
        dE = np.asarray(energies)
        dE[:-1] -= dE[1:]
        dE = dE[:-1] # last element is discarded
        dE = dE[mask]*1000
        return pairs, dE

    def map_follow_multiprocessing(self, passes, n=8):
        """
        Wrapper, that enables multicore processing. Check called function for the description.
        """
        pas = list(np.array_split(np.asarray(passes), n))
        with multiprocessing.Pool(n) as pool:
            results = pool.map(self.map_follow, pas)
        for p in results:
            self.flux += p[0]
            self.DE += p[1]
            self.se_traj += p[2]
        return self.flux, self.DE

    def map_follow(self, passes):
        """
        Get energy losses in the structure per cell

        :param passes: a collection of trajectories
        :return:
        """
        # Segments in all the trajectories deposit energy and emit SEs independently.
        # Thus, their interconnection and relation to a certain trajectory does not have to be preserved.
        # This fact is used to collect all segments and energy losses in single arrays
        # and to vectorize operations on them.

        # Masks that are passed along with trajectories play an important role by
        # letting efficiently sieve out segments that did not have energy loss (i.e those traversing void)
        # Zeros in energy array cause errors as energy is divisor in numerous parts of the algorithm.

        points = [] # coordinate pairs (segments) are first collected here
        dEs = [] # same for energies
        traj_lengths = []
        traj_len = 0
        for one_pass in passes:
            if len(one_pass[1][:]) < 3:
                continue
            pairs, energies = self.__setup_trajectory(one_pass[0][1:], one_pass[1][1:], one_pass[2][1:])
            if pairs.shape[0]:
                points.append(pairs)
                dEs.append(energies)
                traj_len += pairs.shape[0]
                traj_lengths.append(traj_len)
        traj_lengths = np.asarray(traj_lengths)
        segments_all = np.concatenate(points, axis=0)
        dEs_all = np.concatenate(dEs, axis=0)
        self.follow_segment(segments_all, dEs_all)
        self.prep_se_emission(segments_all, dEs_all, traj_lengths-1)
        self.generate_se()
        a=0

        return self.flux, self.DE # has to be returned, as every process (when using multiprocessing) gets its own copy of the whole class and thus does not write to the original