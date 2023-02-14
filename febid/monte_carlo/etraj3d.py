# Default packages
import warnings

# Core packages
import numpy as np
import pyvista as pv

# Axillary packages
from tkinter import filedialog as fd
import timeit

# Local packages
from febid.Structure import Structure
from febid.libraries.vtk_rendering import VTK_Rendering as vr
from febid.monte_carlo import etrajectory as et
from febid.monte_carlo import etrajmap3d as map3d
from febid.monte_carlo.mc_base import MC_Sim_Base, Element, substrates


class MC_Simulation(MC_Sim_Base):
    """
    Monte Carlo simulation main class
    """
    def __init__(self, structure, mc_params):
        self.cell_dim = structure.cell_dimension
        self.grid = structure.deposit
        self.substrate = mc_params['substrate'] = substrates[mc_params['substrate_element']]
        self.deponat = mc_params['deponat'] = Element(mc_params['name'], mc_params['Z'], mc_params['A'], mc_params['rho'], mc_params['e'], mc_params['l'], -1)
        self.pe_sim = et.ETrajectory()
        self.pe_sim.setParameters(structure, mc_params)
        self.se_sim = map3d.ETrajMap3d()
        self.se_sim.setParametrs(structure, mc_params, 0.3)

        self.se_surface_flux = None
        self.beam_heating = None

    def update_structure(self, structure):
        """
        Renew memory addresses of the arrays

        :param structure:
        :return:
        """
        self.pe_sim.grid = self.se_sim.grid = structure.deposit
        self.pe_sim.surface = self.se_sim.surface = structure.surface_bool
        self.se_sim.s_neighb = structure.surface_neighbors_bool
        self.se_surface_flux = np.zeros(structure.shape, dtype=np.int32)
        self.beam_heating = np.zeros(structure.shape)

    def run_simulation(self, y0, x0, heat, N=None):
        """
        Run MC simulation with the beam coordinates

        :param y0: spot y-coordinate
        :param x0: spot x-coordinate
        :param heat: if True, calculate beam heating
        :return: SE surface flux
        """
        if not N:
            if heat:
                N = 20000
            else:
                N = self.pe_sim.N
        norm_factor = self.pe_sim.get_norm_factor(N)
        self.pe_sim.map_wrapper_cy(y0, x0, N)
        self.se_sim.map_follow(self.pe_sim.passes, heat)
        const = norm_factor / self.se_sim.amplifying_factor / self.pe_sim.cell_dim ** 2 / self.se_sim.segment_min_length
        if heat:
            self.beam_heating = self.se_sim.heat * norm_factor / self.pe_sim.cell_dim ** 3
        self.se_surface_flux = np.int32(self.se_sim.flux * const)
        return self.se_surface_flux

    def plot(self, primary_e=True, secondary_flux=True, secondary_e=False, heat_total=False,
             heat_pe=False, heat_se=False, timings=(None,None,None), cam_pos=None):  # plot energy loss and all trajectories
        """
        Show the structure with surface electron flux and electron trajectories

        :return:
        """
        render = vr.Render(self.pe_sim.cell_dim)
        kwargs = {}
        if primary_e:
            pe_trajectories = np.asarray(self.pe_sim.passes, dtype='object')
            kwargs['pe_traj'] = pe_trajectories
        if secondary_flux:
            kwargs['surface_flux'] = self.se_sim.flux
        if secondary_e:
            kwargs['se_traj'] = self.se_sim.coords
        if heat_total:
            kwargs['heat_t'] = self.se_sim.heat
        if heat_pe:
            kwargs['heat_pe'] = self.se_sim.heat_pe
        if heat_se:
            kwargs['heat_se'] = self.se_sim.wasted_se
        if cam_pos:
            kwargs['cam_pos'] = cam_pos
        if timings:
            kwargs['t'] = timings[0]
            kwargs['sim_time'] = timings[1]
            kwargs['beam'] = timings[2]
        cam_pos = render.show_mc_result(self.pe_sim.grid, **kwargs, interactive=False)
        return cam_pos

    def plot_flux_2d(self):
        import matplotlib.pyplot as plt
        summed = np.sum(self.se_surface_flux, 0)
        x, y = np.mgrid[0:summed.shape[1] + 1,
               0:summed.shape[0] + 1]  # +1 because 'shading=flat' requires dropping last column and row
        fig, ax = plt.subplots()
        ax.pcolormesh(x, y, summed, cmap='plasma', shading='flat')
        locator = plt.MultipleLocator(self.se_sim.cell_dim)
        # ax.xaxis.set_major_locator(locator)
        # ax.yaxis.set_major_locator(locator)
        plt.show()


def run_mc_simulation(structure, E0=20, sigma=5, n=1, N=100, pos='center', precursor='Au', Emin=0.1, emission_fraction=0.6, heating=False, params={}, cam_pos=None):
    """
    Create necessary objects and run the MC simulation

    :param structure:
    :param E0:
    :param sigma:
    :param N:
    :param pos:
    :param precursor:
    :param Emin:
    :return:
    """

    mc_config = {'E0': E0,
                 'Emin': Emin,
                 'I0': 1e-10, 'sigma': sigma, 'n': n,
                 'N': N, 'substrate_element': 'Au',
                 'cell_dim': structure.cell_dimension,
                 'emission_fraction': emission_fraction}
    if type(precursor) is not str:
        precursor_config = {'name': precursor["deposit"],
                     'Z': precursor["average_element_number"],
                     'A': precursor["average_element_mol_mass"], 'rho': precursor["average_density"],
                     'e': precursor["SE_emission_activation_energy"], 'l': precursor["SE_mean_free_path"],}
    elif precursor in substrates.keys():
        precursor_config = substrates[precursor]

    mc_config = {**mc_config, **precursor_config}
    sim = MC_Simulation(structure, mc_config)
    x, y = 0, 0
    if pos == 'center':
        x = structure.shape[2]/2 * structure.cell_dimension
        y = structure.shape[1]/2 * structure.cell_dimension
    else:
        x, y = pos
    print(f'{N} PE trajectories took:   \t Energy deposition took:   \t SE preparation took:   \t Flux counting took:')
    start = timeit.default_timer()
    sim.run_simulation(y, x, heating, N)
    print(f'{timeit.default_timer() - start}', end='\t\t')
    args = [True, True, True, True, True, True, params, cam_pos]
    if not heating:
        args[3] = False
        args[4] = False
        args[5] = False
    cam_pos = sim.plot(*args)
    return cam_pos


def mc_simulation():
    """
    Fetch necessary data and start the simulation

    :return:
    """
    #TODO: This standalone Monte Carlo module should provide more data than in FEBID simulation

    print(f'Monte-Carlo electron beam - matter interaction module.\n'
          f'First load desired structure from vtk file, then enter parameters.')
    print(f'Select .vtk file....')
    while True:
        try:
            file = fd.askopenfilename()
            # file = '/Users/sandrik1742/Documents/PycharmProjects/FEBID/_source_samples/electron_beam_monte-carlo/hockeystick.vti'
            vtk_obj = pv.read(file)
        except Exception as e:
            print(f'Unable to read vtk file. {e.args} \n'
                  f'Try again:')
        else:
            print(f'Got file!\n')
            break

    print(f'Input parameters: Beam energy(keV), gauss st. deviation, number of electrons to emit, (beam x position, beam y position) , structure material(i.e. Au)\n'
          f'Note: \'center\' can be used instead of a coordinates pair (x,y) to set the beam to the center')
    E0=read_param('Beam energy', [int, float])
    sigma = read_param('Gauss standard deviation', [int, float])
    N = read_param('Number of electrons', [int])
    pos = read_param('Beam position', [tuple, str], check_string=['center'])
    material = read_param('Structure material', [str], check_string=['Au'])
    print(f'Got paramers!\n')
    run_mc_simulation(vtk_obj, E0, sigma, N, pos, material)
    # run_mc_simulation(vtk_obj, 20, 15, 1000, (12,17), 'Au')


def read_param(name, expected_type, message="Inappropriate input for ", check_string=None):
    """
    Read and parse a parameter from the input

    :param name: name of teh parameter
    :param expected_type: data type
    :param message: error text
    :param check_string: look for a specific string
    :return:
    """
    while True:
        item = input(f'Enter {name}:')
        try:
            result = eval(item)
        except:
            result = item
        if type(result) in expected_type:
            if type(result) is str:
                if check_string is not None:
                    if result in check_string:
                        return result
                    else:
                        warnings.warn("Input does not match any text choice")
                        print(f'Try again')
                        continue
            return result
        else:
            # unlike 'raise Warning()', this does not interrupt code execution
            warnings.warn(message+name)
            print(f'Try again')
            continue


if __name__ == '__main__':
    print("Current script does not have an entry point.....")
    input('Press Enter to exit.')