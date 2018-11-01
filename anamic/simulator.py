import json
import datetime

import numpy as np
from scipy import signal
from scipy import ndimage
import tifffile
import matplotlib.pyplot as plt

from . import structure
from . import viz
from . import transformations


def mt_builder(n_pf, mt_length_nm, taper_length_nm):
    long_dimer_distance = 8  # nm
    
    # Compute the number of rows
    n_rows = int(np.round(mt_length_nm / long_dimer_distance))
    
    # Create the dimers array
    dimers = np.ones((n_pf, n_rows))
    
    # Generate a tapered tip
    dimers = structure.generate_uniform_taper(dimers, taper_length_nm=taper_length_nm)
    return MicrotubuleSimulator(dimers)

class MicrotubuleSimulator():
    """
    Args:
        dimers: Numpy array of dimension 2.
    """
    long_dimer_distance = 8  # nm
    
    def __init__(self, dimers):
        """
        Args:
            dimer: Nmupy array.
        """
        self.dimers = dimers
        
        self.positions = None
        self.discrete_image = None
        self.psf = None
        
        self.metadata = {}
        self.metadata['n_pf'] = self.dimers.shape[0]
        self.metadata['row'] = self.dimers.shape[1]
        
    # Methods to build the microtubule geometry.
        
    def build_positions(self, apply_random_z_rotation=True, show_progress=True):
        """Calculate the x, y and z positions of each dimers.
        
        Args:
            apply_random_z_rotation: boolean.
                Apply a random rotation to 3D positions along the Z axis (microtubule length).
            show_progress: boolean.
                Show a progress bar.
        """
        self.positions = structure.get_dimer_positions(self.dimers, show_progress=show_progress)
        if apply_random_z_rotation:
            self._random_3d_z_rotation()
        
    def _random_3d_z_rotation(self):
        """Apply a random rotation parallell to the surface (along the z axis).
        This is to avoid having the seam always at the same location.
        """
        self.metadata['3d_z_rotation_angle'] = np.random.randn() * 360
        rotation_angle = np.deg2rad(self.metadata['3d_z_rotation_angle'])
        Rz = transformations.rotation_matrix(rotation_angle, [0, 0, 1])
        self.positions[['x', 'y', 'z']] = np.dot(self.positions[['x', 'y', 'z']].values, Rz[:3, :3].T)
        
    def label(self, labeling_ratio):
        """Apply a certain labeling ratio. This will add a column 'labeled'
        to `self.positions`.
        
        Args:
            labeling_ratio: float from 0 to 1.
        """
        self.metadata['labeling_ratio'] = labeling_ratio
        self.positions['labeled'] = np.random.random(self.positions.shape[0]) < labeling_ratio
        
    def project(self):
        """Project the 3D positions (x, y, z) on a 2D plan. The projection is done
        along the microtubule z axis so the microtubule is parallel to the "fake" coverslip.
        Projected coordinates are called `x_proj` and `y_proj`.
        """
        self.positions[['x_proj', 'y_proj']] = self.positions[['x', 'z']].copy()
        
    def random_rotation_projected(self):
        """Apply a random 2D rotation on `x_proj` and `y_proj`. New coordinates are called
        'x_proj_rotated' and 'y_proj_rotated'.
        """
        self.metadata['projected_rotation_angle'] = np.random.randn() * 360
        random_angle = np.deg2rad(self.metadata['projected_rotation_angle'])
        R = transformations.rotation_matrix(random_angle, [0, 0, 1])

        self.positions['x_proj_rotated'] = np.nan
        self.positions['y_proj_rotated'] = np.nan
        self.positions[['x_proj_rotated', 'y_proj_rotated']] = np.dot(self.positions[['x_proj', 'y_proj']], R[:2, :2].T)
        
    # Methods to generate the image
    
    def discretize_position(self, pixel_size=110, x_offset=800, y_offset=800):
        """Discretize dimer positions on an image. Image is stored in `self.discrete_image`.
        `self.positions` will contains two new columns: `x_pixel` and `y_pixel`.
        
        Args:
            pixel_size: float, in nm/pixel.
            x_offset: float, in nm.
            y_offset: float, in nm.
        """
        
        self.metadata['pixel_size'] = pixel_size
        self.metadata['x_offset'] = x_offset
        self.metadata['y_offset'] = y_offset
        
        # Discretize dimers positions onto an image
        x_max = int(np.round(self.positions['x_proj_rotated'].max() + 1))
        x_min = int(np.round(self.positions['x_proj_rotated'].min() - 1))
        y_max = int(np.round(self.positions['y_proj_rotated'].max() + 1))
        y_min = int(np.round(self.positions['y_proj_rotated'].min() - 1))

        x_bins = np.arange(x_min - x_offset, x_max + x_offset, pixel_size)
        y_bins = np.arange(y_min - y_offset, y_max + y_offset, pixel_size)

        # Select visible and labeled dimers
        selected_dimers = self.positions[(self.positions['visible'] == True) & (self.positions['labeled'] == True)]

        # Bin dimers positions to a 2D grid (defined by pixel_size)
        self.discrete_image, _, _ = np.histogram2d(selected_dimers['x_proj_rotated'], selected_dimers['y_proj_rotated'],
                                                   bins=[x_bins, y_bins])

        # Keep the width > height consistant
        if self.discrete_image.shape[1] < self.discrete_image.shape[0]:
            self.discrete_image = self.discrete_image.T
            self.positions[['x_proj', 'y_proj']] = self.positions.loc[:, ['y_proj', 'x_proj']]
            self.positions[['x_proj_rotated', 'y_proj_rotated']] = self.positions.loc[:, ['y_proj_rotated', 'x_proj_rotated']]
            x_bins, y_bins = y_bins, x_bins

        # We also save the dimers positions on the fine grid (unit is pixel)
        # The pixel_shift is not ideal but seems to be necessary...
        pixel_shift = -1
        self.positions.loc[:, 'x_pixel'] = np.digitize(self.positions['x_proj_rotated'], x_bins) + pixel_shift
        self.positions.loc[:, 'y_pixel'] = np.digitize(self.positions['y_proj_rotated'], y_bins) + pixel_shift
  
    def _generate_psf(self, psf_size):
        """Generate a PSF from a Gaussian.
        
        Args:
            psf_size: float, in nm.
        """
        self.metadata['psf_size'] = psf_size
        self.metadata['sigma_pixel'] = psf_size / self.metadata['pixel_size']
        kernel_size_pixel = int(self.metadata['sigma_pixel'] * 10)
        gaussian_kernel_1d = signal.gaussian(kernel_size_pixel, std=self.metadata['sigma_pixel'])
        gaussian_kernel_1d = gaussian_kernel_1d.reshape(kernel_size_pixel, 1)
        self.psf = np.outer(gaussian_kernel_1d, gaussian_kernel_1d)
        return self.psf
    
    def convolve(self, psf_size, noise_factor):
        """Convolve `self.discrete_image` with a PSF and add some Poisson
        noise.
        
        Args:
            psf_size: float, in nm.
            noise_factor: int.
        """
        
        self.metadata['noise_factor'] = noise_factor
        
        self._generate_psf(psf_size=psf_size)
        self.image  = ndimage.convolve(self.discrete_image, self.psf, mode="constant")

        # Add offset signal
        self.image  = self.image + self.image.max() * 0.1

        # Add noise
        noise = np.random.poisson(self.image).astype(float) * noise_factor
        self.image = self.image + noise
        self.image /= 10

    # Methods to visualize positions or images.
    
    def visualize_2d_positions(self, x_feature, y_feature, show_all=True, show_labeled=True,
                               color_feature='pf', marker_size=30, x_offset=400):
        """Visualize 2D dimer positions.
        
        Args:
            x_feature: string, the x feature to be used.
            y_feature: string, the y feature to be used.
            show_all: boolean, show all visible dimers.
            show_labeled: boolean, show only labeled dimers.
            color_feature: string, feature to use to color dimers.
            marker_size: float, size of the dimer marker.
            x_offset: float, offset apply on the X axis to show only the tips.
        """
        
        axs = None
        if show_all and show_labeled:
            fig, axs = plt.subplots(nrows=2, figsize=(18, 6), constrained_layout=True)
        else:
            fig, ax = plt.subplots(figsize=(18, 6), constrained_layout=True)

        if show_all:
            ax = axs[0] if show_labeled else ax
            # Visualize all dimers
            selected_dimers = self.positions[(self.positions['visible'] == True)]
            viz.viz_2d_dimers_positions(ax, selected_dimers,
                                               x_feature=y_feature, y_feature=x_feature,
                                               color_feature=color_feature, marker_size=marker_size,
                                               x_offset=x_offset)

        if show_labeled:
            ax = axs[1] if show_all else ax
            # Visualize only labeled dimers
            selected_dimers = self.positions[(self.positions['visible'] == True) & (self.positions['labeled'] == True)]
            viz.viz_2d_dimers_positions(axs[1], selected_dimers,
                                               x_feature=y_feature, y_feature=x_feature,
                                               color_feature=color_feature, marker_size=marker_size,
                                               x_offset=x_offset)
        return fig
    
    def show_positions(self, color_feature_name='pf', size=0.4):
        # Show 3D position
        return viz.viz_dimer_positions(self.positions, size=size,
                                       color_feature_name=color_feature_name)
    
    def show_psf(self):
        fig, ax = plt.subplots(figsize=(5, 5))
        viz.imshow_colorbar(self.psf, ax)
        
    def show_image(self, tip_marker_size=80):
        fig, ax = plt.subplots(figsize=(8, 8))
        viz.imshow_colorbar(self.image, ax)
        viz.show_tips(ax, self.positions, coordinates_features=['y_pixel', 'x_pixel'],
                             marker_size=tip_marker_size)
    
    # Utility methods
    
    def save_positions(self, fpath):
        """Save `self.positions` to a CSV file.
        """
        self.positions.to_csv(fpath)
    
    def save_metadata(self, fpath):
        """Save `self.metadata` to a JSON file.
        """
        self.metadata['date'] = datetime.datetime.now().isoformat()
        with open(fpath, 'w') as fp:
            json.dump(self.metadata, fp, indent=2, sort_keys=True)
            
    def save_image(self, fpath):
        """Save `self.image` to a TIFF file.
        """
        tifffile.imsave(fpath, self.image)