"""This module contains a class for handling Cartesian raster-band data."""

#
# Copyright (c) Simon Kohaut, Honda Research Institute Europe GmbH
#
# This file is part of ProMis and licensed under the BSD 3-Clause License.
# You should have received a copy of the BSD 3-Clause License along with ProMis.
# If not, see https://opensource.org/license/bsd-3-clause/.
#

# Standard Library
from io import BytesIO
from itertools import product

# Third Party
import matplotlib.pyplot as plt
from matplotlib.transforms import Bbox
from numpy import array, ndarray, sum, uint8, vstack, zeros
from PIL import Image
from sklearn.preprocessing import MinMaxScaler

# ProMis
from promis.geo.location_type import LocationType
from promis.geo.map import CartesianLocation, CartesianMap, PolarLocation, PolarMap
from promis.geo.polygon import CartesianPolygon
from promis.models import GaussianMixture


class RasterBand:

    """A Cartesian raster-band representing map data concerning a location type.

    Args:
        data: The raster band data
        origin: The polar coordinates of this raster-band's center
        width: The width the raster band stretches over in meters
        height: The height the raster band stretches over in meters
    """

    def __init__(self, data: ndarray, origin: PolarLocation, width: float, height: float):
        # Attributes setup
        self.data = data
        self.origin = origin
        self.width = width
        self.height = height

        # Dimension of each pixel in meters
        self.pixel_width = self.width / self.data.shape[0]
        self.pixel_height = self.height / self.data.shape[1]

        # Location of center in meters relative to top-left corner
        self.center_x = self.width / 2
        self.center_y = self.height / 2

        # Precomputes locations of all pixels in Cartesian and polar space
        self.cartesian_locations = {
            index: self.index_to_cartesian(index)
            for index in product(range(data.shape[0]), range(data.shape[1]))
        }
        self.polar_locations = {
            index: self.index_to_polar(index)
            for index in product(range(data.shape[0]), range(data.shape[1]))
        }

    @classmethod
    def from_map(
        cls, map_: PolarMap | CartesianMap, location_type: LocationType, resolution: tuple[int, int]
    ) -> "RasterBand":
        """Takes a PolarMap or CartesianMap to initialize the raster band data.

        Args:
            map_: The map to read from
            location_type: The location type to create a raster-band from
            resolution: The resolution of the raster-band data

        Returns:
            The raster-band with dimensions and data retrieved from reading the map data
        """

        # Attributes setup
        map_ = map_ if isinstance(map_, CartesianMap) else map_.to_cartesian()

        # Create and prepare figure for plotting
        figure, axis = plt.subplots(figsize=(map_.width, map_.height), dpi=1)
        axis.set_aspect("equal")
        axis.set_axis_off()
        axis.set_xlim([-map_.width, map_.width])
        axis.set_ylim([-map_.height, map_.height])

        # Plot all features with this type
        # TODO: Only considers polygons right now
        for feature in map_.features:
            if isinstance(feature, CartesianPolygon) and feature.location_type == location_type:
                feature.plot(axis, facecolor="black")

        # Create a bounding box with the actual map data
        figure.canvas.draw()
        bounding_box = Bbox(
            [[-map_.width / 2, -map_.height / 2], [map_.width / 2, map_.height / 2]]
        )
        bounding_box = bounding_box.transformed(axis.transData).transformed(
            figure.dpi_scale_trans.inverted()
        )

        # Get image from figure and check if it is empty
        raster_band_image = cls._figure_to_image(figure, bounding_box)

        # Clean up
        plt.close(figure)

        # If raster-band contains no data, we can set to zeros
        extrema = raster_band_image.convert("L").getextrema()
        if extrema[0] == extrema[1]:
            data = zeros(resolution)

        # Else we setup the data as numpy array
        else:
            # Resize to specified resolution
            raster_band_image = raster_band_image.resize(resolution)

            # Convert to numpy and normalize from discrete [0, 255] to continuous [0, 1]
            # Since we draw existing features in black on a white background, we invert colors
            # Also drop two of the three sub-bands since all are equal
            data = array(raster_band_image, dtype="float32")
            data = data[:, :, 0]
            data /= 255.0
            data = 1 - data
            data = data.transpose()

        return cls(data, map_.origin, map_.width, map_.height)

    @classmethod
    def from_gaussian_mixture(
        cls,
        gaussian_mixture: GaussianMixture,
        origin: PolarLocation,
        width: float,
        height: float,
        resolution: tuple[int, int],
    ) -> "RasterBand":
        """Compute probabilities from a Gaussian Mixture Model over a Cartesian region.

        Args:
            gaussian_mixture: The Gaussian Mixture Model
            origin: The polar coordinates of this raster-band's center
            width: The width the raster band stretches over in meters
            height: The height the raster band stretches over in meters
            resolution: The resolution of the raster-band data

        Returns:
            The raster-band with data obtained from a Gaussian Mixture Model
        """

        # Start with an empty raster-band
        raster_band = cls(zeros(resolution), origin, width, height)

        # Precompute vectors from pixel center to corners
        top_right_vector = 0.5 * vstack([raster_band.pixel_width, raster_band.pixel_height])
        top_left_vector = 0.5 * vstack([-raster_band.pixel_width, raster_band.pixel_height])
        bottom_right_vector = 0.5 * vstack([raster_band.pixel_width, -raster_band.pixel_height])
        bottom_left_vector = 0.5 * vstack([-raster_band.pixel_width, -raster_band.pixel_height])

        # Compute probability as sum of each mixture component
        # Here, we access the internals of the Gaussians since we can utilize caching this way
        probabilities = zeros((len(gaussian_mixture), resolution[0], resolution[1]))
        for i, gaussian in enumerate(gaussian_mixture):
            # Our cache is a defaultdict that initializes with the CDF of the Gaussian
            # This simplifies going over all indices and caching all CDFs
            cdf_raster: dict[tuple(float, float), float] = {}

            for index in product(range(resolution[0]), range(resolution[1])):
                # Cell coordinates
                location = raster_band.cartesian_locations[index].to_numpy()
                top_right = location + top_right_vector
                top_left = location + top_left_vector
                bottom_right = location + bottom_right_vector
                bottom_left = location + bottom_left_vector

                # Since we want to use these as keys, they need to be hashable
                top_right = tuple(top_right.T[0])
                top_left = tuple(top_left.T[0])
                bottom_right = tuple(bottom_right.T[0])
                bottom_left = tuple(bottom_left.T[0])

                # Compute CDF where needed
                if top_right not in cdf_raster:
                    cdf_raster[top_right] = gaussian.cdf(top_right)
                if top_left not in cdf_raster:
                    cdf_raster[top_left] = gaussian.cdf(top_left)
                if bottom_right not in cdf_raster:
                    cdf_raster[bottom_right] = gaussian.cdf(bottom_right)
                if bottom_left not in cdf_raster:
                    cdf_raster[bottom_left] = gaussian.cdf(bottom_left)

                # Since we use the defaultdict, values will be reused from previous indices
                probabilities[i][index] = gaussian.weight * (
                    cdf_raster[top_right]
                    - cdf_raster[top_left]
                    - cdf_raster[bottom_right]
                    + cdf_raster[bottom_left]
                )

        # Set raster band data from sum of all probability rasters and return
        raster_band.data = sum(probabilities, axis=0)
        return raster_band

    def split(self) -> "list[list[RasterBand]] | RasterBand":
        """TODO"""

        if self.data.shape[0] == 1 or self.data.shape[1] == 1:
            return self

        data_split_x = self.data.shape[0] // 2
        data_split_y = self.data.shape[1] // 2

        if self.width % 2 != 0:
            left_width = (self.width - self.pixel_width) / 2
            right_width = (self.width + self.pixel_width) / 2
            origin_west = -(self.width + self.pixel_width) / 4
            origin_east = (self.width - self.pixel_width) / 4
        else:
            left_width = right_width = self.width / 2
            origin_east = self.width / 4
            origin_west = -origin_east

        if self.height % 2 != 0:
            top_height = (self.height - self.pixel_height) / 2
            bottom_height = (self.height + self.pixel_height) / 2
            origin_north = -(self.height - self.pixel_height) / 4
            origin_south = (self.height + self.pixel_height) / 4
        else:
            top_height = bottom_height = self.height / 2
            origin_south = self.height / 4
            origin_north = -origin_south

        return [
            [
                RasterBand(
                    self.data[:data_split_x, :data_split_y],
                    CartesianLocation(origin_west, origin_south).to_polar(self.origin),
                    left_width,
                    top_height,
                ),
                RasterBand(
                    self.data[:data_split_x, data_split_y:],
                    CartesianLocation(origin_west, origin_north).to_polar(self.origin),
                    left_width,
                    bottom_height,
                ),
            ],
            [
                RasterBand(
                    self.data[data_split_x:, :data_split_y],
                    CartesianLocation(origin_east, origin_south).to_polar(self.origin),
                    right_width,
                    top_height,
                ),
                RasterBand(
                    self.data[data_split_x:, data_split_y:],
                    CartesianLocation(origin_east, origin_north).to_polar(self.origin),
                    right_width,
                    bottom_height,
                ),
            ],
        ]

    def index_to_cartesian(self, index: tuple[int, int]) -> CartesianLocation:
        """Computes the cartesian location of an index of this raster-band.

        Args:
            The raster-band index to compute from

        Returns:
            The cartesian location of this index
        """

        # Compute cartesian location relative to origin
        cartesian_location = CartesianLocation(
            east=(self.pixel_width / 2) + index[0] * self.pixel_width - self.center_x,
            north=-((self.pixel_height / 2) + index[1] * self.pixel_height) + self.center_y,
        )

        return cartesian_location

    def index_to_polar(self, index: tuple[int, int]) -> PolarLocation:
        """Computes the polar location of an index of this raster-band.

        Args:
            The raster-band index to compute from

        Returns:
            The polar location of this index
        """

        return self.index_to_cartesian(index).to_polar(self.origin)

    def to_image(self) -> Image:
        image_data = MinMaxScaler(feature_range=(0, 255)).fit_transform(self.data)
        return Image.fromarray(uint8(image_data.transpose()))

    def save_as_image(self, path: str):
        """Saves the raster-band data as image file.

        Args:
            path: The path with filename to write to
        """

        self.to_image().save(path)

    def save_as_csv(self, path: str, time: str | None = None, append=False):
        """Saves the raster-band data as comma separated values.

        Args:
            path: The path with filename to write to
        """

        # Create a new file to write to
        mode = "a" if append else "w"
        with open(path, mode, encoding="utf-8") as csv_file:
            # Set the csv header
            header = "latitude, longitude, value"
            if time is not None:
                header += ", datetime"

            # Write header to file
            if not append:
                csv_file.write(header + "\n")

            # For each data point we write the polar location and respective raster-band value
            for x, y in product(range(self.data.shape[0]), range(self.data.shape[1])):
                # Write value of this indexed polar location
                polar_location = self.index_to_polar((x, y))
                csv_file.write(
                    f"{polar_location.latitude}, {polar_location.longitude}, {self.data[x, y]:.20f}"
                )

                # Append datetime if available
                if time is not None:
                    csv_file.write(f", {time}\n")

                # Ensure newline
                csv_file.write("\n")

    @staticmethod
    def _figure_to_image(figure, bounding_box=None) -> Image.Image:
        """Convert a Matplotlib figure to a PIL Image.

        Args:
            figure: The Matplotlib figure
            bounding_box: The image region to export

        Returns:
            The PIL Image exported from the figure
        """

        # Save the figure to an in-memory buffer
        buffer = BytesIO()
        figure.savefig(buffer, dpi=1, bbox_inches=bounding_box)
        buffer.seek(0)

        # Open the image from in-memory buffer and return
        return Image.open(buffer)
