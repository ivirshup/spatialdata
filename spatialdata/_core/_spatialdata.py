from __future__ import annotations

import hashlib
import os
from collections.abc import Generator
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Optional, Union

import zarr
from anndata import AnnData
from dask.dataframe.core import DataFrame as DaskDataFrame
from geopandas import GeoDataFrame
from multiscale_spatial_image.multiscale_spatial_image import MultiscaleSpatialImage
from ome_zarr.io import parse_url
from ome_zarr.types import JSONDict
from pyarrow.parquet import read_table
from spatial_image import SpatialImage

from spatialdata._core.core_utils import SpatialElement, get_dims
from spatialdata._core.models import (
    Image2DModel,
    Image3DModel,
    Labels2DModel,
    Labels3DModel,
    PointsModel,
    ShapesModel,
    TableModel,
)
from spatialdata._io.write import (
    write_image,
    write_labels,
    write_points,
    write_shapes,
    write_table,
)
from spatialdata._logging import logger
from spatialdata._types import ArrayLike
from spatialdata.utils import get_backing_files

if TYPE_CHECKING:
    from spatialdata._core._spatial_query import BaseSpatialRequest

# schema for elements
Label2D_s = Labels2DModel()
Label3D_s = Labels3DModel()
Image2D_s = Image2DModel()
Image3D_s = Image3DModel()
Shape_s = ShapesModel()
Point_s = PointsModel()
Table_s = TableModel()


class SpatialData:
    """
    The SpatialData object.

    The SpatialData object is a modular container for arbitrary combinations of spatial elements. The elements
    can be accesses separately and are stored as standard types (:class:`anndata.AnnData`,
    :class:`geopandas.GeoDataFrame`, :class:`xarray.DataArray`).


    Parameters
    ----------
    images
        Dict of 2D and 3D image elements. The following parsers are available: :class:`~spatialdata.Image2DModel`,
        :class:`~spatialdata.Image3DModel`.
    labels
        Dict of 2D and 3D labels elements. Labels are regions, they can't contain annotation, but they can be
        annotated by a table. The following parsers are available: :class:`~spatialdata.Labels2DModel`,
        :class:`~spatialdata.Labels3DModel`.
    points
        Dict of points elements. Points can contain annotations. The following parsers is available:
        :class:`~spatialdata.PointsModel`.
    shapes
        Dict of 2D shapes elements (circles, polygons, multipolygons). Shapes are regions, they can't contain annotation but they
        can be annotated by a table. The following parsers is available: :class:`~spatialdata.ShapesModel`.
    table
        AnnData table containing annotations for regions (labels and shapes). The following parsers is
        available: :class:`~spatialdata.TableModel`.

    Notes
    -----
    The spatial elements are stored with standard types:

        - images and labels are stored as :class:`spatial_image.SpatialImage` or :class:`multiscale_spatial_image.MultiscaleSpatialImage` objects, which are respectively equivalent to :class:`xarray.DataArray` and to a :class:`datatree.DataTree` of :class:`xarray.DataArray` objects.
        - points are stored as :class:`dask.dataframe.DataFrame` objects.
        - shapes are stored as :class:`geopandas.GeoDataFrame`.
        - the table are stored as :class:`anndata.AnnData` objects, with the spatial coordinates stored in the obsm slot.

    The table can annotate regions (shapesor labels) and can be used to store additional information.
    Points are not regions but 0-dimensional locations. They can't be annotated by a table, but they can store
    annotation directly.

    The elements need to pass a validation step. To construct valid elements you can use the parsers that we
    provide (:class:`~spatialdata.Image2DModel`, :class:`~spatialdata.Image3DModel`, :class:`~spatialdata.Labels2DModel`, :class:`~spatialdata.Labels3DModel`, :class:`~spatialdata.PointsModel`, :class:`~spatialdata.ShapesModel`, :class:`~spatialdata.TableModel`).
    """

    _images: dict[str, Union[SpatialImage, MultiscaleSpatialImage]] = MappingProxyType({})  # type: ignore[assignment]
    _labels: dict[str, Union[SpatialImage, MultiscaleSpatialImage]] = MappingProxyType({})  # type: ignore[assignment]
    _points: dict[str, DaskDataFrame] = MappingProxyType({})  # type: ignore[assignment]
    _shapes: dict[str, AnnData] = MappingProxyType({})  # type: ignore[assignment]
    _table: Optional[AnnData] = None
    path: Optional[str] = None

    def __init__(
        self,
        images: dict[str, Union[SpatialImage, MultiscaleSpatialImage]] = MappingProxyType({}),  # type: ignore[assignment]
        labels: dict[str, Union[SpatialImage, MultiscaleSpatialImage]] = MappingProxyType({}),  # type: ignore[assignment]
        points: dict[str, DaskDataFrame] = MappingProxyType({}),  # type: ignore[assignment]
        shapes: dict[str, AnnData] = MappingProxyType({}),  # type: ignore[assignment]
        table: Optional[AnnData] = None,
    ) -> None:
        self.path = None
        if images is not None:
            self._images: dict[str, Union[SpatialImage, MultiscaleSpatialImage]] = {}
            for k, v in images.items():
                self._add_image_in_memory(name=k, image=v)

        if labels is not None:
            self._labels: dict[str, Union[SpatialImage, MultiscaleSpatialImage]] = {}
            for k, v in labels.items():
                self._add_labels_in_memory(name=k, labels=v)

        if shapes is not None:
            self._shapes: dict[str, AnnData] = {}
            for k, v in shapes.items():
                self._add_shapes_in_memory(name=k, shapes=v)

        if points is not None:
            self._points: dict[str, DaskDataFrame] = {}
            for k, v in points.items():
                self._add_points_in_memory(name=k, points=v)

        if table is not None:
            Table_s.validate(table)
            self._table = table

        self._query = QueryManager(self)

    @property
    def query(self) -> QueryManager:
        return self._query

    def _add_image_in_memory(
        self, name: str, image: Union[SpatialImage, MultiscaleSpatialImage], overwrite: bool = False
    ) -> None:
        if name in self._images:
            if not overwrite:
                raise ValueError(f"Image {name} already exists in the dataset.")
        ndim = len(get_dims(image))
        if ndim == 3:
            Image2D_s.validate(image)
            self._images[name] = image
        elif ndim == 4:
            Image3D_s.validate(image)
            self._images[name] = image
        else:
            raise ValueError("Only czyx and cyx images supported")

    def _add_labels_in_memory(
        self, name: str, labels: Union[SpatialImage, MultiscaleSpatialImage], overwrite: bool = False
    ) -> None:
        if name in self._labels:
            if not overwrite:
                raise ValueError(f"Labels {name} already exists in the dataset.")
        ndim = len(get_dims(labels))
        if ndim == 2:
            Label2D_s.validate(labels)
            self._labels[name] = labels
        elif ndim == 3:
            Label3D_s.validate(labels)
            self._labels[name] = labels
        else:
            raise ValueError(f"Only yx and zyx labels supported, got {ndim} dimensions")

    def _add_shapes_in_memory(self, name: str, shapes: GeoDataFrame, overwrite: bool = False) -> None:
        if name in self._shapes:
            if not overwrite:
                raise ValueError(f"Shapes {name} already exists in the dataset.")
        Shape_s.validate(shapes)
        self._shapes[name] = shapes

    def _add_points_in_memory(self, name: str, points: DaskDataFrame, overwrite: bool = False) -> None:
        if name in self._points:
            if not overwrite:
                raise ValueError(f"Points {name} already exists in the dataset.")
        Point_s.validate(points)
        self._points[name] = points

    def is_backed(self) -> bool:
        """Check if the data is backed by a Zarr storage or it is in-memory."""
        return self.path is not None

    # TODO: from a commennt from Giovanni: consolite somewhere in a future PR (luca: also _init_add_element could be cleaned)
    def _get_group_for_element(self, name: str, element_type: str) -> zarr.Group:
        store = parse_url(self.path, mode="r+").store
        root = zarr.group(store=store)
        assert element_type in ["images", "labels", "points", "polygons", "shapes"]
        element_type_group = root.require_group(element_type)
        element_group = element_type_group.require_group(name)
        return element_group

    def _init_add_element(self, name: str, element_type: str, overwrite: bool) -> zarr.Group:
        if self.path is None:
            # in the future we can relax this, but this ensures that we don't have objects that are partially backed
            # and partially in memory
            raise RuntimeError(
                "The data is not backed by a Zarr storage. In order to add new elements after "
                "initializing a SpatialData object you need to call SpatialData.write() first"
            )
        store = parse_url(self.path, mode="r+").store
        root = zarr.group(store=store)
        assert element_type in ["images", "labels", "points", "shapes"]
        # not need to create the group for labels as it is already handled by ome-zarr-py
        if element_type != "labels":
            if element_type not in root:
                elem_group = root.create_group(name=element_type)
            else:
                elem_group = root[element_type]
        if overwrite:
            if element_type == "labels":
                if element_type in root:
                    elem_group = root[element_type]
            if name in elem_group:
                del elem_group[name]
        else:
            # bypass is to ensure that elem_group is defined. I don't want to define it as None but either having it
            # or not having it, so if the code tries to access it and it should not be there, it will raise an error
            bypass = False
            if element_type == "labels":
                if element_type in root:
                    elem_group = root[element_type]
                else:
                    bypass = True
            if not bypass:
                if name in elem_group:
                    raise ValueError(f"Element {name} already exists, use overwrite=True to overwrite it")

        if element_type != "labels":
            return elem_group
        else:
            return root

    def _locate_spatial_element(self, element: SpatialElement) -> tuple[str, str]:
        found: list[SpatialElement] = []
        found_element_type: list[str] = []
        found_element_name: list[str] = []
        for element_type in ["images", "labels", "points", "shapes"]:
            for element_name, element_value in getattr(self, element_type).items():
                if id(element_value) == id(element):
                    found.append(element_value)
                    found_element_type.append(element_type)
                    found_element_name.append(element_name)
        if len(found) == 0:
            raise ValueError("Element not found in the SpatialData object.")
        elif len(found) > 1:
            raise ValueError(
                f"Element found multiple times in the SpatialData object. Found {len(found)} elements with names: {found_element_name}, and types: {found_element_type}"
            )
        assert len(found_element_name) == 1
        assert len(found_element_type) == 1
        return found_element_name[0], found_element_type[0]

    def contains_element(self, element: SpatialElement, raise_exception: bool = False) -> bool:
        """
        Check if the spatial element is contained in the SpatialData object.

        Parameters
        ----------
        element
            The spatial element to check
        raise_exception
            If True, raise an exception if the element is not found. If False, return False if the element is not found.

        Returns
        -------
        True if the element is found; False otherwise (if raise_exception is False).

        """
        try:
            self._locate_spatial_element(element)
            return True
        except ValueError as e:
            if raise_exception:
                raise e
            else:
                return False

    def _write_transformations_to_disk(self, element: SpatialElement) -> None:
        from spatialdata._core._spatialdata_ops import get_transformation

        transformations = get_transformation(element, get_all=True)
        assert isinstance(transformations, dict)
        found_element_name, found_element_type = self._locate_spatial_element(element)

        if self.path is not None:
            group = self._get_group_for_element(name=found_element_name, element_type=found_element_type)
            axes = get_dims(element)
            if isinstance(element, SpatialImage) or isinstance(element, MultiscaleSpatialImage):
                from spatialdata._io.write import (
                    overwrite_coordinate_transformations_raster,
                )

                overwrite_coordinate_transformations_raster(group=group, axes=axes, transformations=transformations)
            elif (
                isinstance(element, DaskDataFrame) or isinstance(element, GeoDataFrame) or isinstance(element, AnnData)
            ):
                from spatialdata._io.write import (
                    overwrite_coordinate_transformations_non_raster,
                )

                overwrite_coordinate_transformations_non_raster(group=group, axes=axes, transformations=transformations)
            else:
                raise ValueError("Unknown element type")

    def filter_by_coordinate_system(
        self, coordinate_system: Union[str, list[str]], filter_table: bool = True
    ) -> SpatialData:
        """
        Filter the SpatialData by one (or a list of) coordinate system.

        This returns a SpatialData object with the elements containing a transformation mapping to the specified
        coordinate system(s).

        Parameters
        ----------
        coordinate_system
            The coordinate system(s) to filter by.
        filter_table
            If True (default), the table will be filtered to only contain regions of an element belonging to the specified
            coordinate system(s).

        Returns
        -------
        The filtered SpatialData.
        """
        from spatialdata._core._spatialdata_ops import get_transformation

        elements: dict[str, dict[str, SpatialElement]] = {}
        element_paths_in_coordinate_system = []
        if isinstance(coordinate_system, str):
            coordinate_system = [coordinate_system]
        for element_type, element_name, element in self._gen_elements():
            transformations = get_transformation(element, get_all=True)
            assert isinstance(transformations, dict)
            for cs in coordinate_system:
                if cs in transformations:
                    if element_type not in elements:
                        elements[element_type] = {}
                    elements[element_type][element_name] = element
                    element_paths_in_coordinate_system.append(f"{element_type}/{element_name}")

        if filter_table:
            table_mapping_metadata = self.table.uns[TableModel.ATTRS_KEY]
            region_key = table_mapping_metadata[TableModel.REGION_KEY_KEY]
            table = self.table[self.table.obs[region_key].isin(element_paths_in_coordinate_system)].copy()
            table.uns[TableModel.ATTRS_KEY][TableModel.REGION_KEY] = table.obs[region_key].unique().tolist()
        else:
            table = self.table

        return SpatialData(**elements, table=table)

    def transform_element_to_coordinate_system(
        self, element: SpatialElement, target_coordinate_system: str
    ) -> SpatialElement:
        """
        Transform an element to a given coordinate system.

        Parameters
        ----------
        element
            The element to transform.
        target_coordinate_system
            The target coordinate system.

        Returns
        -------
        The transformed element.
        """
        from spatialdata._core._spatialdata_ops import (
            get_transformation_between_coordinate_systems,
        )

        t = get_transformation_between_coordinate_systems(self, element, target_coordinate_system)
        transformed = t.transform(element)
        return transformed

    def transform_to_coordinate_system(
        self, target_coordinate_system: str, filter_by_coordinate_system: bool = True
    ) -> SpatialData:
        """
        Transform the SpatialData to a given coordinate system.

        Parameters
        ----------
        target_coordinate_system
            The target coordinate system.
        filter_by_coordinate_system
            Whether to filter the SpatialData by the target coordinate system before transforming.
            If set to True, only elements with a coordinate transforming to the specified coordinate system
            will be present in the returned SpatialData object. Default value is True.

        Returns
        -------
        The transformed SpatialData.
        """
        if filter_by_coordinate_system:
            sdata = self.filter_by_coordinate_system(target_coordinate_system, filter_table=False)
        else:
            sdata = self
        elements: dict[str, dict[str, SpatialElement]] = {}
        for element_type, element_name, element in sdata._gen_elements():
            transformed = sdata.transform_element_to_coordinate_system(element, target_coordinate_system)
            if element_type not in elements:
                elements[element_type] = {}
            elements[element_type][element_name] = transformed
        return SpatialData(**elements, table=sdata.table)

    def add_image(
        self,
        name: str,
        image: Union[SpatialImage, MultiscaleSpatialImage],
        storage_options: Optional[Union[JSONDict, list[JSONDict]]] = None,
        overwrite: bool = False,
    ) -> None:
        """
        Add an image to the SpatialData object.

        Parameters
        ----------
        name
            Key to the element inside the SpatialData object.
        image
            The image to add, the object needs to pass validation (see :class:`~spatialdata.Image2DModel` and :class:`~spatialdata.Image3DModel`).
        storage_options
            Storage options for the Zarr storage.
            See https://zarr.readthedocs.io/en/stable/api/storage.html for more details.
        overwrite
            If True, overwrite the element if it already exists.

        Notes
        -----
        If the SpatialData object is backed by a Zarr storage, the image will be written to the Zarr storage.
        """
        if self.is_backed():
            files = get_backing_files(image)
            assert self.path is not None
            target_path = os.path.realpath(os.path.join(self.path, "images", name))
            if target_path in files:
                raise ValueError(
                    "Cannot add the image to the SpatialData object because it would overwrite an element that it is"
                    "using for backing. See more here: https://github.com/scverse/spatialdata/pull/138"
                )
            self._add_image_in_memory(name=name, image=image, overwrite=overwrite)
            # old code to support overwriting the backing file
            # with tempfile.TemporaryDirectory() as tmpdir:
            #     store = parse_url(Path(tmpdir) / "data.zarr", mode="w").store
            #     root = zarr.group(store=store)
            #     write_image(
            #         image=self.images[name],
            #         group=root,
            #         name=name,
            #         storage_options=storage_options,
            #     )
            #     src_element_path = Path(store.path) / name
            #     assert isinstance(self.path, str)
            #     tgt_element_path = Path(self.path) / "images" / name
            #     if os.path.isdir(tgt_element_path) and overwrite:
            #         element_store = parse_url(tgt_element_path, mode="w").store
            #         _ = zarr.group(store=element_store, overwrite=True)
            #         element_store.close()
            #     pathlib.Path(tgt_element_path).mkdir(parents=True, exist_ok=True)
            #     for file in os.listdir(str(src_element_path)):
            #         src_file = src_element_path / file
            #         tgt_file = tgt_element_path / file
            #         os.rename(src_file, tgt_file)
            # from spatialdata._io.read import _read_multiscale
            #
            # # reload the image from the Zarr storage so that now the element is lazy loaded, and most importantly,
            # # from the correct storage
            # image = _read_multiscale(str(tgt_element_path), raster_type="image")
            # self._add_image_in_memory(name=name, image=image, overwrite=True)
            elem_group = self._init_add_element(name=name, element_type="images", overwrite=overwrite)
            write_image(
                image=self.images[name],
                group=elem_group,
                name=name,
                storage_options=storage_options,
            )
            from spatialdata._io.read import _read_multiscale

            # reload the image from the Zarr storage so that now the element is lazy loaded, and most importantly,
            # from the correct storage
            assert elem_group.path == "images"
            path = Path(elem_group.store.path) / "images" / name
            image = _read_multiscale(path, raster_type="image")
            self._add_image_in_memory(name=name, image=image, overwrite=True)
        else:
            self._add_image_in_memory(name=name, image=image, overwrite=overwrite)

    def add_labels(
        self,
        name: str,
        labels: Union[SpatialImage, MultiscaleSpatialImage],
        storage_options: Optional[Union[JSONDict, list[JSONDict]]] = None,
        overwrite: bool = False,
    ) -> None:
        """
        Add labels to the SpatialData object.

        Parameters
        ----------
        name
            Key to the element inside the SpatialData object.
        labels
            The labels (masks) to add, the object needs to pass validation (see :class:`~spatialdata.Labels2DModel` and :class:`~spatialdata.Labels3DModel`).
        storage_options
            Storage options for the Zarr storage.
            See https://zarr.readthedocs.io/en/stable/api/storage.html for more details.
        overwrite
            If True, overwrite the element if it already exists.

        Notes
        -----
        If the SpatialData object is backed by a Zarr storage, the image will be written to the Zarr storage.
        """
        if self.is_backed():
            files = get_backing_files(labels)
            assert self.path is not None
            target_path = os.path.realpath(os.path.join(self.path, "labels", name))
            if target_path in files:
                raise ValueError(
                    "Cannot add the image to the SpatialData object because it would overwrite an element that it is"
                    "using for backing. We are considering changing this behavior to allow the overwriting of "
                    "elements used for backing. If you would like to support this use case please leave a comment on "
                    "https://github.com/scverse/spatialdata/pull/138"
                )
            self._add_labels_in_memory(name=name, labels=labels, overwrite=overwrite)
            # old code to support overwriting the backing file
            # with tempfile.TemporaryDirectory() as tmpdir:
            #     store = parse_url(Path(tmpdir) / "data.zarr", mode="w").store
            #     root = zarr.group(store=store)
            #     write_labels(
            #         labels=self.labels[name],
            #         group=root,
            #         name=name,
            #         storage_options=storage_options,
            #     )
            #     src_element_path = Path(store.path) / "labels" / name
            #     assert isinstance(self.path, str)
            #     tgt_element_path = Path(self.path) / "labels" / name
            #     if os.path.isdir(tgt_element_path) and overwrite:
            #         element_store = parse_url(tgt_element_path, mode="w").store
            #         _ = zarr.group(store=element_store, overwrite=True)
            #         element_store.close()
            #     pathlib.Path(tgt_element_path).mkdir(parents=True, exist_ok=True)
            #     for file in os.listdir(str(src_element_path)):
            #         src_file = src_element_path / file
            #         tgt_file = tgt_element_path / file
            #         os.rename(src_file, tgt_file)
            # from spatialdata._io.read import _read_multiscale
            #
            # # reload the labels from the Zarr storage so that now the element is lazy loaded, and most importantly,
            # # from the correct storage
            # labels = _read_multiscale(str(tgt_element_path), raster_type="labels")
            # self._add_labels_in_memory(name=name, labels=labels, overwrite=True)
            elem_group = self._init_add_element(name=name, element_type="labels", overwrite=overwrite)
            write_labels(
                labels=self.labels[name],
                group=elem_group,
                name=name,
                storage_options=storage_options,
            )
            # reload the labels from the Zarr storage so that now the element is lazy loaded, and most importantly,
            # from the correct storage
            from spatialdata._io.read import _read_multiscale

            # just a check to make sure that things go as expected
            assert elem_group.path == ""
            path = Path(elem_group.store.path) / "labels" / name
            labels = _read_multiscale(path, raster_type="labels")
            self._add_labels_in_memory(name=name, labels=labels, overwrite=True)
        else:
            self._add_labels_in_memory(name=name, labels=labels, overwrite=overwrite)

    def add_points(
        self,
        name: str,
        points: DaskDataFrame,
        overwrite: bool = False,
    ) -> None:
        """
        Add points to the SpatialData object.

        Parameters
        ----------
        name
            Key to the element inside the SpatialData object.
        points
            The points to add, the object needs to pass validation (see :class:`spatialdata.PointsModel`).
        storage_options
            Storage options for the Zarr storage.
            See https://zarr.readthedocs.io/en/stable/api/storage.html for more details.
        overwrite
            If True, overwrite the element if it already exists.

        Notes
        -----
        If the SpatialData object is backed by a Zarr storage, the image will be written to the Zarr storage.
        """
        if self.is_backed():
            files = get_backing_files(points)
            assert self.path is not None
            target_path = os.path.realpath(os.path.join(self.path, "points", name, "points.parquet"))
            if target_path in files:
                raise ValueError(
                    "Cannot add the image to the SpatialData object because it would overwrite an element that it is"
                    "using for backing. We are considering changing this behavior to allow the overwriting of "
                    "elements used for backing. If you would like to support this use case please leave a comment on "
                    "https://github.com/scverse/spatialdata/pull/138"
                )
            self._add_points_in_memory(name=name, points=points, overwrite=overwrite)
            # old code to support overwriting the backing file
            # with tempfile.TemporaryDirectory() as tmpdir:
            #     store = parse_url(Path(tmpdir) / "data.zarr", mode="w").store
            #     root = zarr.group(store=store)
            #     write_points(
            #         points=self.points[name],
            #         group=root,
            #         name=name,
            #     )
            #     src_element_path = Path(store.path) / name
            #     assert isinstance(self.path, str)
            #     tgt_element_path = Path(self.path) / "points" / name
            #     if os.path.isdir(tgt_element_path) and overwrite:
            #         element_store = parse_url(tgt_element_path, mode="w").store
            #         _ = zarr.group(store=element_store, overwrite=True)
            #         element_store.close()
            #     pathlib.Path(tgt_element_path).mkdir(parents=True, exist_ok=True)
            #     for file in os.listdir(str(src_element_path)):
            #         src_file = src_element_path / file
            #         tgt_file = tgt_element_path / file
            #         os.rename(src_file, tgt_file)
            # from spatialdata._io.read import _read_points
            #
            # # reload the points from the Zarr storage so that now the element is lazy loaded, and most importantly,
            # # from the correct storage
            # points = _read_points(str(tgt_element_path))
            # self._add_points_in_memory(name=name, points=points, overwrite=True)
            elem_group = self._init_add_element(name=name, element_type="points", overwrite=overwrite)
            write_points(
                points=self.points[name],
                group=elem_group,
                name=name,
            )
            # reload the points from the Zarr storage so that now the element is lazy loaded, and most importantly,
            # from the correct storage
            from spatialdata._io.read import _read_points

            assert elem_group.path == "points"

            path = Path(elem_group.store.path) / "points" / name
            points = _read_points(path)
            self._add_points_in_memory(name=name, points=points, overwrite=True)
        else:
            self._add_points_in_memory(name=name, points=points, overwrite=overwrite)

    def add_shapes(
        self,
        name: str,
        shapes: AnnData,
        overwrite: bool = False,
    ) -> None:
        """
        Add shapes to the SpatialData object.

        Parameters
        ----------
        name
            Key to the element inside the SpatialData object.
        shapes
            The shapes to add, the object needs to pass validation (see :class:`~spatialdata.ShapesModel`).
        storage_options
            Storage options for the Zarr storage.
            See https://zarr.readthedocs.io/en/stable/api/storage.html for more details.
        overwrite
            If True, overwrite the element if it already exists.

        Notes
        -----
        If the SpatialData object is backed by a Zarr storage, the image will be written to the Zarr storage.
        """
        self._add_shapes_in_memory(name=name, shapes=shapes, overwrite=overwrite)
        if self.is_backed():
            elem_group = self._init_add_element(name=name, element_type="shapes", overwrite=overwrite)
            write_shapes(
                shapes=self.shapes[name],
                group=elem_group,
                name=name,
            )
            # no reloading of the file storage since the AnnData is not lazy loaded

    def write(
        self,
        file_path: Union[str, Path],
        storage_options: Optional[Union[JSONDict, list[JSONDict]]] = None,
        overwrite: bool = False,
    ) -> None:
        """Write the SpatialData object to Zarr."""
        if isinstance(file_path, str):
            file_path = Path(file_path)
        assert isinstance(file_path, Path)

        if self.is_backed() and self.path != file_path:
            logger.info(f"The Zarr file used for backing will now change from {self.path} to {file_path}")

        # old code to support overwriting the backing file
        # target_path = None
        # tmp_zarr_file = None
        if os.path.exists(file_path):
            if parse_url(file_path, mode="r") is None:
                raise ValueError(
                    "The target file path specified already exists, and it has been detected to not be "
                    "a Zarr store. Overwriting non-Zarr stores is not supported to prevent accidental "
                    "data loss."
                )
            if not overwrite and self.path != str(file_path):
                raise ValueError("The Zarr store already exists. Use `overwrite=True` to overwrite the store.")
            elif str(file_path) == self.path:
                raise ValueError(
                    "The file path specified is the same as the one used for backing. "
                    "Overwriting the backing file is not supported to prevent accidental data loss."
                    "We are discussing how to support this use case in the future, if you would like us to "
                    "support it please leave a comment on https://github.com/scverse/spatialdata/pull/138"
                )
                # old code to support overwriting the backing file
                # else:
                #     target_path = tempfile.TemporaryDirectory()
                #     tmp_zarr_file = Path(target_path.name) / "data.zarr"

        # old code to support overwriting the backing file
        # if target_path is None:
        #     store = parse_url(file_path, mode="w").store
        # else:
        #     store = parse_url(tmp_zarr_file, mode="w").store
        # store = parse_url(file_path, mode="w").store
        # root = zarr.group(store=store)
        store = parse_url(file_path, mode="w").store

        root = zarr.group(store=store, overwrite=overwrite)
        store.close()

        # old code to support overwriting the backing file
        # if target_path is None:
        #     self.path = str(file_path)
        # else:
        #     self.path = str(tmp_zarr_file)
        self.path = str(file_path)
        try:
            if len(self.images):
                root.create_group(name="images")
                # add_image_in_memory will delete and replace the same key in self.images, so we need to make a copy of the
                # keys. Same for the other elements
                # keys = list(self.images.keys())
                keys = self.images.keys()
                from spatialdata._io.read import _read_multiscale

                for name in keys:
                    elem_group = self._init_add_element(name=name, element_type="images", overwrite=overwrite)
                    write_image(
                        image=self.images[name],
                        group=elem_group,
                        name=name,
                        storage_options=storage_options,
                    )

                    # reload the image from the Zarr storage so that now the element is lazy loaded, and most importantly,
                    # from the correct storage
                    element_path = Path(self.path) / "images" / name
                    image = _read_multiscale(element_path, raster_type="image")
                    self._add_image_in_memory(name=name, image=image, overwrite=True)

            if len(self.labels):
                root.create_group(name="labels")
                # keys = list(self.labels.keys())
                keys = self.labels.keys()
                from spatialdata._io.read import _read_multiscale

                for name in keys:
                    elem_group = self._init_add_element(name=name, element_type="labels", overwrite=overwrite)
                    write_labels(
                        labels=self.labels[name],
                        group=elem_group,
                        name=name,
                        storage_options=storage_options,
                    )

                    # reload the labels from the Zarr storage so that now the element is lazy loaded, and most importantly,
                    # from the correct storage
                    element_path = Path(self.path) / "labels" / name
                    labels = _read_multiscale(element_path, raster_type="labels")
                    self._add_labels_in_memory(name=name, labels=labels, overwrite=True)

            if len(self.points):
                root.create_group(name="points")
                # keys = list(self.points.keys())
                keys = self.points.keys()
                from spatialdata._io.read import _read_points

                for name in keys:
                    elem_group = self._init_add_element(name=name, element_type="points", overwrite=overwrite)
                    write_points(
                        points=self.points[name],
                        group=elem_group,
                        name=name,
                    )
                    element_path = Path(self.path) / "points" / name

                    # reload the points from the Zarr storage so that now the element is lazy loaded, and most importantly,
                    # from the correct storage
                    points = _read_points(element_path)
                    self._add_points_in_memory(name=name, points=points, overwrite=True)

            if len(self.shapes):
                root.create_group(name="shapes")
                # keys = list(self.shapes.keys())
                keys = self.shapes.keys()
                for name in keys:
                    elem_group = self._init_add_element(name=name, element_type="shapes", overwrite=overwrite)
                    write_shapes(
                        shapes=self.shapes[name],
                        group=elem_group,
                        name=name,
                    )
                    # no reloading of the file storage since the AnnData is not lazy loaded

            if self.table is not None:
                elem_group = root.create_group(name="table")
                write_table(table=self.table, group=elem_group, name="table")

        except Exception as e:  # noqa: B902
            self.path = None
            raise e

        # old code to support overwriting the backing file
        # if target_path is not None:
        #     if os.path.isdir(file_path):
        #         assert overwrite is True
        #         store = parse_url(file_path, mode="w").store
        #         _ = zarr.group(store=store, overwrite=overwrite)
        #         store.close()
        #     for file in os.listdir(str(tmp_zarr_file)):
        #         assert isinstance(tmp_zarr_file, Path)
        #         src_file = tmp_zarr_file / file
        #         tgt_file = file_path / file
        #         os.rename(src_file, tgt_file)
        #     target_path.cleanup()
        #
        #     self.path = str(file_path)
        #     # elements that need to be reloaded are: images, labels, points
        #     # non-backed elements don't need to be reloaded: table, shapes, polygons
        #
        #     from spatialdata._io.read import _read_multiscale, _read_points
        #
        #     for element_type in ["images", "labels", "points"]:
        #         names = list(self.__getattribute__(element_type).keys())
        #         for name in names:
        #             path = file_path / element_type / name
        #             if element_type in ["images", "labels"]:
        #                 raster_type = element_type if element_type == "labels" else "image"
        #                 element = _read_multiscale(str(path), raster_type=raster_type)  # type: ignore[arg-type]
        #             elif element_type == "points":
        #                 element = _read_points(str(path))
        #             else:
        #                 raise ValueError(f"Unknown element type {element_type}")
        #             self.__getattribute__(element_type)[name] = element
        assert isinstance(self.path, str)

    @property
    def table(self) -> AnnData:
        """
        Return the table.

        Returns
        -------
        The table.
        """
        return self._table

    @table.setter
    def table(self, table: AnnData) -> None:
        """
        Set the table of a SpatialData object in a object that doesn't contain a table.

        Parameters
        ----------
        table
            The table to set.

        Notes
        -----
        If a table is already present, it needs to be removed first.
        The table needs to pass validation (see :class:`~spatialdata.TableModel`).
        If the SpatialData object is backed by a Zarr storage, the table will be written to the Zarr storage.
        """
        TableModel().validate(table)
        if self.table is not None:
            raise ValueError("The table already exists. Use del sdata.table to remove it first.")
        self._table = table
        if self.is_backed():
            store = parse_url(self.path, mode="r+").store
            root = zarr.group(store=store)
            write_table(table=self.table, group=root, name="table")

    @table.deleter
    def table(self) -> None:
        """Delete the table."""
        self._table = None
        if self.is_backed():
            store = parse_url(self.path, mode="r+").store
            root = zarr.group(store=store)
            del root["table"]

    @staticmethod
    def read(file_path: str) -> SpatialData:
        from spatialdata._io.read import read_zarr

        sdata = read_zarr(file_path)
        return sdata

    @property
    def images(self) -> dict[str, Union[SpatialImage, MultiscaleSpatialImage]]:
        """Return images as a Dict of name to image data."""
        return self._images

    @property
    def labels(self) -> dict[str, Union[SpatialImage, MultiscaleSpatialImage]]:
        """Return labels as a Dict of name to label data."""
        return self._labels

    @property
    def points(self) -> dict[str, DaskDataFrame]:
        """Return points as a Dict of name to point data."""
        return self._points

    @property
    def shapes(self) -> dict[str, AnnData]:
        """Return shapes as a Dict of name to shape data."""
        return self._shapes

    @property
    def coordinate_systems(self) -> list[str]:
        from spatialdata._core._spatialdata_ops import get_transformation

        all_cs = set()
        gen = self._gen_elements_values()
        for obj in gen:
            transformations = get_transformation(obj, get_all=True)
            assert isinstance(transformations, dict)
            for cs in transformations:
                all_cs.add(cs)
        return list(all_cs)

    def _non_empty_elements(self) -> list[str]:
        """Get the names of the elements that are not empty.

        Returns
        -------
        non_empty_elements
            The names of the elements that are not empty.
        """
        all_elements = ["images", "labels", "points", "shapes", "table"]
        return [
            element
            for element in all_elements
            if (getattr(self, element) is not None) and (len(getattr(self, element)) > 0)
        ]

    def __repr__(self) -> str:
        return self._gen_repr()

    def _gen_repr(
        self,
    ) -> str:
        """
        Generate a string representation of the SpatialData object.
        Returns
        -------
            The string representation of the SpatialData object.
        """

        def rreplace(s: str, old: str, new: str, occurrence: int) -> str:
            li = s.rsplit(old, occurrence)
            return new.join(li)

        def h(s: str) -> str:
            return hashlib.md5(repr(s).encode()).hexdigest()

        descr = "SpatialData object with:"

        non_empty_elements = self._non_empty_elements()
        last_element_index = len(non_empty_elements) - 1
        for attr_index, attr in enumerate(non_empty_elements):
            last_attr = True if (attr_index == last_element_index) else False
            attribute = getattr(self, attr)

            descr += f"\n{h('level0')}{attr.capitalize()}"
            if isinstance(attribute, AnnData):
                descr += f"{h('empty_line')}"
                descr_class = attribute.__class__.__name__
                descr += f"{h('level1.0')}{attribute!r}: {descr_class} {attribute.shape}"
                descr = rreplace(descr, h("level1.0"), "    └── ", 1)
            else:
                for k, v in attribute.items():
                    descr += f"{h('empty_line')}"
                    descr_class = v.__class__.__name__
                    if attr == "shapes":
                        descr += f"{h(attr + 'level1.1')}{k!r}: {descr_class} " f"shape: {v.shape} (2D shapes)"
                    elif attr == "points":
                        if len(v.dask.layers) == 1:
                            name, layer = v.dask.layers.items().__iter__().__next__()
                            if "read-parquet" in name:
                                t = layer.creation_info["args"]
                                assert isinstance(t, tuple)
                                assert len(t) == 1
                                parquet_file = t[0]
                                table = read_table(parquet_file)
                                length = len(table)
                            else:
                                length = len(v)
                        else:
                            length = len(v)
                        if length > 0:
                            n = len(get_dims(v))
                            dim_string = f"({n}D points)"
                        else:
                            dim_string = ""
                        assert len(v.shape) == 2
                        shape_str = f"({length}, {v.shape[1]})"
                        # if the above is slow, use this (this actually doesn't show the length of the dataframe)
                        # shape_str = (
                        #     "("
                        #     + ", ".join([str(dim) if not isinstance(dim, Delayed) else "<Delayed>" for dim in v.shape])
                        #     + ")"
                        # )
                        descr += f"{h(attr + 'level1.1')}{k!r}: {descr_class} " f"with shape: {shape_str} {dim_string}"
                    else:
                        if isinstance(v, SpatialImage):
                            descr += f"{h(attr + 'level1.1')}{k!r}: {descr_class}[{''.join(v.dims)}] {v.shape}"
                        elif isinstance(v, MultiscaleSpatialImage):
                            shapes = []
                            dims: Optional[str] = None
                            for pyramid_level in v.keys():
                                dataset_names = list(v[pyramid_level].keys())
                                assert len(dataset_names) == 1
                                dataset_name = dataset_names[0]
                                vv = v[pyramid_level][dataset_name]
                                shape = vv.shape
                                if dims is None:
                                    dims = "".join(vv.dims)
                                shapes.append(shape)
                            descr += (
                                f"{h(attr + 'level1.1')}{k!r}: {descr_class}[{dims}] " f"{', '.join(map(str, shapes))}"
                            )
                        else:
                            raise TypeError(f"Unknown type {type(v)}")
            if last_attr is True:
                descr = descr.replace(h("empty_line"), "\n  ")
            else:
                descr = descr.replace(h("empty_line"), "\n│ ")

        descr = rreplace(descr, h("level0"), "└── ", 1)
        descr = descr.replace(h("level0"), "├── ")

        for attr in ["images", "labels", "points", "table", "shapes"]:
            descr = rreplace(descr, h(attr + "level1.1"), "    └── ", 1)
            descr = descr.replace(h(attr + "level1.1"), "    ├── ")

        from spatialdata._core._spatialdata_ops import get_transformation

        descr += "\nwith coordinate systems:\n"
        for cs in self.coordinate_systems:
            descr += f"▸ {cs}\n"
            gen = self._gen_elements()
            elements_in_cs = []
            for k, name, obj in gen:
                transformations = get_transformation(obj, get_all=True)
                assert isinstance(transformations, dict)
                coordinate_systems = transformations.keys()
                if cs in coordinate_systems:
                    elements_in_cs.append(f"/{k}/{name}")
            if len(elements_in_cs) > 0:
                descr += f'    with elements: {", ".join(elements_in_cs)}\n'
        return descr

    def _gen_elements_values(self) -> Generator[SpatialElement, None, None]:
        for element_type in ["images", "labels", "points", "shapes"]:
            d = getattr(SpatialData, element_type).fget(self)
            yield from d.values()

    def _gen_elements(self) -> Generator[tuple[str, str, SpatialElement], None, None]:
        for element_type in ["images", "labels", "points", "shapes"]:
            d = getattr(SpatialData, element_type).fget(self)
            for k, v in d.items():
                yield element_type, k, v


class QueryManager:
    """Perform queries on SpatialData objects"""

    def __init__(self, sdata: SpatialData):
        self._sdata = sdata

    def bounding_box(
        self,
        axes: tuple[str, ...],
        min_coordinate: ArrayLike,
        max_coordinate: ArrayLike,
        target_coordinate_system: str,
        filter_table: bool = True,
    ) -> SpatialData:
        """Perform a bounding box query on the SpatialData object.

        Parameters
        ----------
        axes
            The axes the bounding box is defined in.
        min_coordinate
            The minimum coordinates of the bounding box.
        max_coordinate
            The maximum coordinates of the bounding box.
        target_coordinate_system
            The coordinate system the bounding box is defined in.
        filter_table
            If True, the table is filtered to only contain rows that are annotating individual regions contained in the bounding box.
        Returns
        -------
        The SpatialData object containing the requested data.
        Elements with no valid data are omitted.
        """
        from spatialdata._core._spatial_query import bounding_box_query

        return bounding_box_query(  # type: ignore[return-value]
            self._sdata,
            axes=axes,
            min_coordinate=min_coordinate,
            max_coordinate=max_coordinate,
            target_coordinate_system=target_coordinate_system,
            filter_table=filter_table,
        )

    def __call__(self, request: BaseSpatialRequest) -> SpatialData:
        from spatialdata._core._spatial_query import BoundingBoxRequest

        if isinstance(request, BoundingBoxRequest):
            # TODO: filter table is on by default, shall we put filter_table inside the request?
            return self.bounding_box(**request.to_dict(), filter_table=True)
        else:
            raise TypeError("unknown request type")
