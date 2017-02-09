from __future__ import absolute_import, division, print_function

import time
import json
import threading
from collections import defaultdict, namedtuple, Iterable
import numpy as np
from lcm import LCM
from robotlocomotion import viewer2_comms_t
from director.thirdparty import transformations


def to_lcm(data):
    msg = viewer2_comms_t()
    msg.utime = data["utime"]
    msg.format = "treeviewer_json"
    msg.format_version_major = 1
    msg.format_version_minor = 0
    msg.data = json.dumps(data)
    msg.num_bytes = len(msg.data)
    return msg


def serialize_transform(tform):
    return {
        "translation": list(transformations.translation_from_matrix(tform)),
        "quaternion": list(transformations.quaternion_from_matrix(tform))
    }


class GeometryData(object):
    __slots__ = ["geometry", "color", "transform"]

    def __init__(self, geometry, color=(1., 1., 1., 1.), transform=np.eye(4)):
        self.geometry = geometry
        self.color = color
        self.transform = transform

    def serialize(self):
        params = self.geometry.serialize()
        params["color"] = list(self.color)
        params["transform"] = serialize_transform(self.transform)
        return params


class BaseGeometry(object):
    def serialize(self):
        raise NotImplementedError()


class Box(BaseGeometry, namedtuple("Box", ["lengths"])):
    def serialize(self):
        return {
            "type": "box",
            "lengths": list(self.lengths)
        }


class Sphere(BaseGeometry, namedtuple("Sphere", ["radius"])):
    def serialize(self):
        return {
            "type": "sphere",
            "radius": self.radius
        }


class Ellipsoid(BaseGeometry, namedtuple("Ellipsoid", ["radii"])):
    def serialize(self):
        return {
            "type": "ellipsoid",
            "radii": list(self.radii)
        }


class Cylinder(BaseGeometry, namedtuple("Cylinder", ["length", "radius"])):
    def serialize(self):
        return {
            "type": "cylinder",
            "length": self.length,
            "radius": self.radius
        }


class Triad(BaseGeometry, namedtuple("Triad", [])):
    def serialize(self):
        return {
            "type": "triad"
        }


class LazyTree(object):
    __slots__ = ["geometries", "transform", "children"]

    def __init__(self, geometries=None, transform=np.eye(4)):
        if geometries is None:
            geometries = []
        self.geometries = geometries
        self.transform = transform
        self.children = defaultdict(lambda: LazyTree())

    def __getitem__(self, item):
        return self.children[item]

    def getdescendant(self, path):
        t = self
        for p in path:
            t = t[p]
        return t

    def descendants(self, prefix=tuple()):
        result = []
        for (key, val) in self.children.items():
            childpath = prefix + (key,)
            result.append(childpath)
            result.extend(val.descendants(childpath))
        return result


class CommandQueue(object):
    def __init__(self):
        self.draw = set()
        self.load = set()
        self.delete = set()

    def isempty(self):
        return not (self.draw or self.load or self.delete)

    def empty(self):
        self.draw = set()
        self.load = set()
        self.delete = set()


class Visualizer(object):
    """
    A Visualizer is a lightweight object that contains a CoreVisualizer and a
    path. The CoreVisualizer does all of the work of storing geometries and
    publishing LCM messages. By storing the path in the Visualizer instance,
    we make it easy to do things like store or pass a Visualizer that draws to
    a sub-part of the viewer tree.
    Many Visualizer objects can all share the same CoreVisualizer.
    """
    __slots__ = ["core", "path"]

    def __init__(self, path=None, lcm=None, core=None):
        if core is None:
            core = CoreVisualizer(lcm)
        if path is None:
            path = tuple()
        else:
            if isinstance(path, str):
                path = tuple(path.split("/"))
                if not path[0]:
                    path = tuple([p for p in path if p])
        self.core = core
        self.path = path

    def load(self, geomdata):
        """
        Set the geometries at this visualizer's path to the given
        geomdata (replacing whatever was there before).
        geomdata can be any one of:
          * a single BaseGeometry
          * a single GeometryData
          * a collection of any combinations of BaseGeometry and GeometryData
        """
        self.core.load(self.path, geomdata)
        return self

    def draw(self, tform):
        """
        Set the transform for this visualizer's path (and, implicitly,
        any descendants of that path).
        tform should be a 4x4 numpy array representing a homogeneous transform
        """
        self.core.draw(self.path, tform)

    def delete(self):
        """
        Delete the geometry at this visualizer's path.
        """
        self.core.delete(self.path)

    def __getitem__(self, path):
        """
        Indexing into a visualizer returns a new visualizer with the given
        path appended to this visualizer's path.
        """
        return Visualizer(path=self.path + (path,),
                          lcm=self.core.lcm,
                          core=self.core)

    def start_handler(self):
        """
        Start a Python thread that will subscribe to messages from the remote
        viewer and handle those responses. This enables automatic reloading of
        geometry into the viewer if, for example, the viewer is restarted
        later.
        """
        self.core.start_handler()


class CoreVisualizer(object):
    def __init__(self, lcm=None):
        if lcm is None:
            lcm = LCM()
        self.lcm = lcm
        self.tree = LazyTree()
        self.queue = CommandQueue()
        self.publish_immediately = True
        self.lcm.subscribe("DIRECTOR_TREE_VIEWER_RESPONSE",
                           self._handle_response)
        self.handler_thread = None

    def _handler_loop(self):
        while True:
            self.lcm.handle()

    def start_handler(self):
        if self.handler_thread is not None:
            return
        self.handler_thread = threading.Thread(
            target=self._handler_loop)
        self.handler_thread.daemon = True
        self.handler_thread.start()

    def _handle_response(self, channel, msgdata):
        msg = viewer2_comms_t.decode(msgdata)
        data = json.loads(msg.data)
        if data["status"] == 0:
            pass
        elif data["status"] == 1:
            for path in self.tree.descendants():
                self.queue.load.add(path)
                self.queue.draw.add(path)
        else:
            raise ValueError(
                "Unhandles response from viewer: {}".format(msg.data))

    def load(self, path, geomdata):
        if isinstance(geomdata, BaseGeometry):
            self._load(path, [GeometryData(geomdata)])
        elif isinstance(geomdata, Iterable):
            self._load(path, geomdata)
        else:
            self._load(path, [geomdata])

    def _load(self, path, geoms):
        converted_geom_data = []
        for geom in geoms:
            if isinstance(geom, GeometryData):
                converted_geom_data.append(geom)
            else:
                converted_geom_data.append(GeometryData(geom))
        self.tree.getdescendant(path).geometries = converted_geom_data
        self.queue.load.add(path)
        self._maybe_publish()

    def draw(self, path, tform):
        self.tree.getdescendant(path).transform = tform
        self.queue.draw.add(path)
        self._maybe_publish()

    def delete(self, path):
        if not path:
            self.tree = LazyTree()
        else:
            t = self.tree.getdescendant(path[:-1])
            del t.children[path[-1]]
        self.queue.delete.add(path)
        self._maybe_publish()

    def _maybe_publish(self):
        if self.publish_immediately:
            self.publish()

    def publish(self):
        if not self.queue.isempty():
            data = self.serialize_queue()
            msg = to_lcm(data)
            self.lcm.publish("DIRECTOR_TREE_VIEWER_REQUEST", msg.encode())
            self.queue.empty()

    def serialize_queue(self):
        delete = []
        load = []
        draw = []
        for path in self.queue.delete:
            delete.append({"path": path})
        for path in self.queue.load:
            geoms = self.tree.getdescendant(path).geometries
            if geoms:
                load.append({
                    "path": path,
                    "geometries": [geom.serialize() for geom in geoms]
                })
        for path in self.queue.draw:
            draw.append({
                "path": path,
                "transform": serialize_transform(
                    self.tree.getdescendant(path).transform)
            })
        return {
            "utime": int(time.time() * 1e6),
            "delete": delete,
            "load": load,
            "draw": draw
        }


if __name__ == '__main__':
    # We can provide an initial path if we want
    vis = Visualizer(path="/root/folder1")

    # Start a thread to handle responses from the viewer. Doing this enables
    # the automatic reloading of missing geometry if the viewer is restarted.
    vis.start_handler()

    vis["boxes"].load(
        [GeometryData(Box([1, 1, 1]),
         color=np.random.rand(4),
         transform=transformations.translation_matrix([x, -2, 0]))
         for x in range(10)])

    # Index into the visualizer to get a sub-tree. vis.__getitem__ is lazily
    # implemented, so these sub-visualizers come into being as soon as they're
    # asked for
    vis = vis["group1"]

    box_vis = vis["box"]
    sphere_vis = vis["sphere"]

    box = Box([1, 1, 1])
    geom = GeometryData(box, color=[0, 1, 0, 0.5])
    box_vis.load(geom)

    sphere_vis.load(Sphere(0.5))
    sphere_vis.draw(transformations.translation_matrix([1, 0, 0]))

    vis["test"].load(Triad())
    vis["test"].draw(transformations.concatenate_matrices(
        transformations.rotation_matrix(1.0, [0, 0, 1]),
        transformations.translation_matrix([-1, 0, 1])))

    # the triad geometry is reloaded, but it keeps
    # the transform from the last draw call.  is that
    # a bug?  should a geometry reload also reset the
    # transform?
    vis["test"].load(Triad())

    # bug, the sphere is loaded and replaces the previous
    # geometry but it is not drawn with the correct color mode
    vis["test"].load(Sphere(0.5))


    for theta in np.linspace(0, 2 * np.pi, 100):
        vis.draw(transformations.rotation_matrix(theta, [0, 0, 1]))
        time.sleep(0.01)

    #vis.delete()
