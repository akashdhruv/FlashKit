"""Create an initial flow field (block) using interpolated simulation data."""

# type annotations
from __future__ import annotations
from typing import cast, Optional

# standard libraries
import os

# internal libraries
from ..core.error import LibraryError
from ..core.parallel import Index, safe
from ..core.progress import Bar
from ..core.tools import first_true
from ..library.create_grid import read_coords
from ..resources import CONFIG
from ..support.files import H5Manager
from ..support.grid import axisMesh, axisUniqueIndex, get_blocks, get_grids, get_shapes
from ..support.types import N, Coords, Grids, Shapes

# external libraries
import numpy
import h5py # type: ignore
from scipy.interpolate import interpn # type: ignore

# define public interface
__all__ = ['interp_blocks', 'SimulationData']

# define configuration constants (internal)
BNAME = CONFIG['create']['block']['name']
FACES = CONFIG['create']['block']['grids'][1:]
METHOD = CONFIG['create']['interp']['method']
class SimulationData:
    axisNumProcs: tuple[int, int, int]
    blkPath: str
    bndboxes: N
    centers: N
    grids: Grids
    ndim: int
    numProcs: int
    shapes: Shapes
    sizes: tuple[int, int, int]

    def __init__(self, *, axis: tuple[int, int, int], block: str, boxes: N, centers: N, grids: Grids, 
                 ndim: int, procs: int, ranges: list[tuple[int, int]], shapes: Shapes, sizes: tuple[int, int, int]) -> None:
        self.axisNumProcs = axis
        self.blkPath = block
        self.bndboxes = boxes.copy()
        self.centers = centers.copy()
        self.grids = grids
        self.ndim = ndim
        self.numProcs = int(procs)
        self.ranges = ranges
        self.shapes = shapes
        self.sizes = sizes

    def blocks_from_bbox(self, box: N) -> list[int]:
        """Return all blocks that at are least partially overlaped by the bounding box."""
        overlaps = lambda ll, lh, hl, hh : not ((hh < ll) or (hl > lh))
        return [blk for blk, bb in enumerate(self.bndboxes)
                if all(overlaps(*low, *high) for low, high in zip(bb, box))]  

    def centers_unique(self, blocks: Optional[list[int]] = None) -> list[N]:
        """Return all unique block centers by axis, or for the specific blocks."""
        sliced = slice(None) if blocks is None else blocks
        return [numpy.unique(self.centers[sliced, axis]) for axis in range(3)]

    def extent_unique(self, blocks: Optional[list[int]] = None) -> list[int]:
        """Return the extent (i.e., number of) of all unique block centers by axis, or for the specific blocks."""
        return [len(axis) for axis in self.centers_unique(blocks)]

    def index_unique(self, blocks: Optional[list[int]] = None) -> list[list[int]]:
        """Return the list of indexes (i.e., [[i,j,k], ...] coordinates) to all unique block centers, or for the specific blocks."""
        unique = self.centers_unique(blocks)
        return [[numpy.where(unique[axis] == coord)[0][0] for axis, coord in enumerate(block)] for block in self.centers[blocks]]

    def index_flatten(self, blocks: Optional[list[int]] = None) -> list[N]:
        """Return the flattened list of unique indexes by axis (i.e., [[i, ...], [j, ...], [k, ...]] coordinates) to all unique block centers, or for the specific blocks."""
        sliced = slice(None) if blocks is None else blocks
        return [numpy.unique([numpy.where(self.centers_unique()[axis] == coord[axis])[0][0] for coord in self.centers[sliced]]) for axis in range(3)]

    def face_shift(self, face: str) -> tuple[int, int, int]:
        """Return the shift (i.e., extra number of points) per axis for the particular face location."""
        return {FACES[0]: (1, 0, 0), FACES[1]: (0, 1, 0), FACES[2]: (0, 0, 1)}.get(face, (0, 0, 0))

    def face_flat_shape(self, *, blocks: Optional[list[int]] = None, face: str) -> list[int]:
        """Return the flattened (i.e., as a single block) shape for the particular face location of all unique block centers, of for the specific blocks."""
        return [extent * size + shift for extent, shift, size in zip(self.extent_unique(blocks), self.face_shift(face), self.sizes)]

    @classmethod
    def from_plot_files(cls, *, basename: str, grid: str, path: str, plot: str, step: int):

        plotfile = os.path.join(path, basename + plot + f'{step:04}')
        with h5py.File(plotfile, 'r') as file:
            scalars = list(file['integer scalars'])
            runtime = list(file['integer runtime parameters'])
            realpar = list(file['real runtime parameters'])
            numProcs = first_true(scalars, lambda l: 'globalnumblocks' in str(l[0]))[1]
            axisNumProcs = (
                    first_true(runtime, lambda l: 'iprocs' in str(l[0]))[1],
                    first_true(runtime, lambda l: 'jprocs' in str(l[0]))[1],
                    first_true(runtime, lambda l: 'kprocs' in str(l[0]))[1])
            sizes = (
                    first_true(scalars, lambda l: 'nxb' in str(l[0]))[1],
                    first_true(scalars, lambda l: 'nyb' in str(l[0]))[1],
                    first_true(scalars, lambda l: 'nzb' in str(l[0]))[1])
            ranges = [(
                first_true(realpar, lambda l: 'xmin' in str(l[0]))[1],
                first_true(realpar, lambda l: 'xmax' in str(l[0]))[1]),(
                first_true(realpar, lambda l: 'ymin' in str(l[0]))[1],
                first_true(realpar, lambda l: 'ymax' in str(l[0]))[1]),(
                first_true(realpar, lambda l: 'zmin' in str(l[0]))[1],
                first_true(realpar, lambda l: 'zmax' in str(l[0]))[1])]
            bndboxes = file['bounding box'][()]    
            centers = file['coordinates'][()]
            ndim = first_true(scalars, lambda l: 'dimensionality' in str(l[0]))[1]

        gridfile = os.path.join(path, basename + grid + '0000')
        with h5py.File(gridfile, 'r') as file:
            faxes = (file[axis][()] for axis in ('xxxf', 'yyyf', 'zzzf'))
            uinds = axisUniqueIndex(*axisNumProcs)
            coords = cast(Coords, tuple(numpy.append(a[i][:,:-1].flatten(), a[-1,-1]) if a is not None else None for a, i in zip(faxes, uinds)))
            grids = get_grids(coords=coords, ndim=ndim, procs=axisNumProcs, sizes=sizes)
            shapes = get_shapes(ndim=ndim, procs=axisNumProcs, sizes=sizes)

        return cls(axis=axisNumProcs, block=plotfile, boxes=bndboxes, centers=centers, grids=grids, ndim=ndim, procs=numProcs, ranges=ranges, shapes=shapes, sizes=sizes)

    @classmethod
    def from_options(cls, *, block: Optional[str] = None, coords: Optional[Coords] = None, ndim: int, path: str, procs: tuple[int, int, int], sizes: tuple[int, int, int]):

        blockfile = os.path.join(path, BNAME if block is None else block)
        axisNumProcs, _ = axisMesh(*procs)
        numProcs = int(numpy.prod(axisNumProcs))
        if coords is None: coords = read_coords(path=path, ndim=ndim)
        ranges = [(coords[0][0], coords[0][-1]), (coords[1][0], coords[1][-1]), (0, 1) if ndim == 2 else (coords[2][0], coords[2][-1])]
        shapes = get_shapes(ndim=ndim, procs=procs, sizes=sizes)
        grids = get_grids(coords=coords, ndim=ndim, procs=procs, sizes=sizes)
        centers, boxes = get_blocks(coords=coords, ndim=ndim, procs=procs, sizes=sizes)

        return cls(axis=axisNumProcs.tolist(), block=blockfile, boxes=boxes, centers=centers, grids=grids, ndim=ndim, procs=numProcs, ranges=ranges, shapes=shapes, sizes=sizes)

@safe
def interp_blocks(*, destination: SimulationData, source: SimulationData, flows: dict[str, tuple[str, str, str]], nofile: bool, context: Bar) -> dict[str, N]:
    """Interpolate desired initial flow fields from a simulation output to another computional grid."""
    
    # create grid init parameters for parallelizing blocks 
    index = Index.from_simple(destination.numProcs)
    mesh_width = index.mesh_width(destination.axisNumProcs)

    # check that source and destination are compatible
    if destination.ndim != source.ndim:
        raise LibraryError('Incompatible source and destination grids for interpolation!')

     # open input and output files for performing the interpolation (writing the data as we go is most memory efficient)
    with H5Manager(source.blkPath, 'r', force=True) as input_file, \
            H5Manager(destination.blkPath, 'w-', clean=True, nofile=nofile) as output_file, \
            context(index.size) as progress:

        # create datasets in output file
        output = {}
        for field, (location, _, _) in flows.items():
            output_file.create_dataset(field, shape=destination.shapes[location], dtype=float)
            output[field] = numpy.empty((index.size, ) + destination.shapes[location][1:], numpy.double)
        
        # interpolate over assigned blocks
        for step, (block, width, bbox) in enumerate(zip(index.range, mesh_width, destination.bndboxes[index.range])):

            # get blocks in the low grid that overlay the high grid
            blocks = source.blocks_from_bbox(bbox)

            # gather necessary information to flatten source data from low grid
            index_uniq = source.index_unique(blocks)
            index_flat = source.index_flatten(blocks)

            # interpolate each field for the working block
            for field, (face, source_field, source_face) in flows.items():

                # calculate flattened source data shape on low grid -- cannot use interpn w/ repeats
                source_shift = source.face_shift(source_face)
                source_shape = source.face_flat_shape(blocks=blocks, face=source_face)

                if source.ndim == 3:
                
                    # interpolate cell center fields
                    xxx = numpy.unique(source.grids[source_face][0][index_flat[0]].flatten()) # type: ignore
                    yyy = numpy.unique(source.grids[source_face][1][index_flat[1]].flatten()) # type: ignore
                    zzz = numpy.unique(source.grids[source_face][2][index_flat[2]].flatten()) # type: ignore
                    values = numpy.empty(source_shape[::-1], dtype=float)
                    for (i, j, k), source_block in zip(index_uniq, blocks):
                        il, ih = i * source.sizes[0], (i + 1) * source.sizes[0] + source_shift[0]
                        jl, jh = j * source.sizes[1], (j + 1) * source.sizes[1] + source_shift[1]
                        kl, kh = k * source.sizes[2], (k + 1) * source.sizes[2] + source_shift[2]
                        values[kl:kh, jl:jh, il:ih] = input_file.read(source_field)[source_block]

                    x = destination.grids[face][0][width[0], None, None, :] # type: ignore
                    y = destination.grids[face][1][width[1], None, :, None] # type: ignore
                    z = destination.grids[face][2][width[2], :, None, None] # type: ignore
                
                    output[field][step] = numpy.maximum(numpy.minimum(
                        interpn((zzz, yyy, xxx), values, (z, y, x), method=METHOD, bounds_error=False, fill_value=None),
                        values.max()), values.min())
                    output_file.write_partial(field, output[field][step], block=block, index=index) 

                elif source.ndim == 2:
                    
                    # interpolate cell center fields
                    xxx = numpy.unique(source.grids[source_face][0][index_flat[0]].flatten()) # type: ignore
                    yyy = numpy.unique(source.grids[source_face][1][index_flat[1]].flatten()) # type: ignore
                    values = numpy.empty(source_shape[1::-1], dtype=float)
                    for (i, j, _), source_block in zip(index_uniq, blocks):
                        il, ih = i * source.sizes[0], (i + 1) * source.sizes[0] + source_shift[0]
                        jl, jh = j * source.sizes[1], (j + 1) * source.sizes[1] + source_shift[1]
                        values[jl:jh, il:ih] = input_file.read(source_field)[source_block, 0]

                    x = destination.grids[face][0][width[0], None, :] # type: ignore
                    y = destination.grids[face][1][width[1], :, None] # type: ignore
                
                    output[field][step] = numpy.maximum(numpy.minimum(
                        interpn((yyy, xxx), values, (y, x), method=METHOD, bounds_error=False, fill_value=None),
                        values.max()), values.min())[None, :, :]
                    output_file.write_partial(field, output[field][step], block=block, index=index) 

                else:
                    pass

            progress()
            
    return output