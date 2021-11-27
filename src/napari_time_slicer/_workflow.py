

from ._function import napari_experimental_provide_function
from ._dock_widget import napari_experimental_provide_dock_widget

from napari.types import ImageData, LabelsData
import napari
import warnings
import numpy as np
from toolz import curry
from typing import Callable
from functools import wraps
import inspect
from qtpy.QtCore import QTimer

from napari._qt.qthreading import thread_worker
import time

METADATA_WORKFLOW_VALID_KEY = "workflow_valid"

class Workflow():
    """
    The workflow class encapsulates a dictionary that works as dask-task-graph.
    """

    def __init__(self):
        self._tasks = {}

    def set(self, name, func_or_data, *args, **kwargs):
        if not callable(func_or_data):
            self._tasks[name] = func_or_data
            return

        sig = inspect.signature(func_or_data)
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()

        self._tasks[name] = tuple([func_or_data] + [value for key, value in bound.arguments.items()])

    def remove(self, name):
        if name in self._tasks.keys():
            self._tasks.pop(name)

    def get(self, name):
        return dask_get(self._tasks, name)

    def get_task(self, name):
        return self._tasks[name]

    def roots(self):
        """
        Return all names of images that have no pre-processing steps.
        """
        origins = []
        for result, task in self._tasks.items():
            for source in task:
                if isinstance(source, str):
                    if not source in list(self._tasks.keys()):
                        if source not in origins:
                            origins.append(source)
        return origins

    def followers_of(self, item):
        """
        Return all names of images that are produces out of a given image.
        """
        followers = []
        for result, task in self._tasks.items():
            for source in task:
                if isinstance(source, str):
                    if source == item:
                        if result not in followers:
                            followers.append(result)
        return followers

    def sources_of(self, item):
        """
        Returns all names of images that need to be there to produce a given image.
        """
        if item not in self._tasks.keys():
            return []
        task = self._tasks[item]
        return [i for i in task if isinstance(i, str)]

    def leafs(self):
        """
        Returns all image names that have no further processing steps.
        """
        return [l for l in self._tasks.keys() if len(self.followers_of(l)) == 0]

    def __str__(self):
        out = "Workflow:\n"
        for result, task in self._tasks.items():
            out = out + result + " <- "+ str(task) + "\n"
        return out



class WorkflowManager():
    """
    The workflow manager is attached to a given napari Viewer once any Workflow step is executed.
    """

    @classmethod
    def install(cls, viewer: napari.Viewer):
        """
        Installs a workflow manager to a given napari Viewer (if not done earlier already) and returns it.
        """
        if not hasattr(WorkflowManager, "viewers_managers"):
            WorkflowManager.viewers_managers = {}

        if not viewer in WorkflowManager.viewers_managers.keys():
           WorkflowManager.viewers_managers[viewer] = WorkflowManager(viewer)
        return WorkflowManager.viewers_managers[viewer]

    def __init__(self, viewer: napari.Viewer):
        self.viewer = viewer
        self.workflow = Workflow()

        self._register_events_to_viewer(viewer)

        # The thread workwer will run in the background and check if images have to be recomputed.
        @thread_worker
        def loop_run():
           while True:  # endless loop
               time.sleep(0.2)
               yield self._update_invalid_layer()

        worker = loop_run()

        # in case some layer was updated by the thread worker, this function will receive the new data
        def update_layer(whatever):
            if whatever is not None:
                name, data = whatever
                if _viewer_has_layer(self.viewer, name):
                    self.viewer.layers[name].data = data

        # Start the loop
        worker.yielded.connect(update_layer)
        worker.start()

    def _update_invalid_layer(self):
        layer = self._search_first_invalid_layer(self.workflow.roots())
        if layer is None:
            return
        print("Detected invalid layer. Recomputing", layer.name)
        layer.data = np.asarray(self._compute(layer.name))
        print("Recomputing done", layer.name)

    def _compute(self, name):
        task = list(self.workflow.get_task(name)).copy()
        function = task[0]
        arguments = task[1:]
        for i in range(len(arguments)):
            a = arguments[i]
            if isinstance(a, str):
                if _viewer_has_layer(self.viewer, a):
                    arguments[i] = self.viewer.layers[a].data
        return function(*arguments)

    def _search_first_invalid_layer(self, items):
        for i in items:
            if _viewer_has_layer(self.viewer, i):
                layer = self.viewer.layers[i]
                if _layer_invalid(layer):
                    return layer
        for i in items:
            invalid_follower = self._search_first_invalid_layer(self.workflow.followers_of(i))
            if invalid_follower is not None:
                return invalid_follower

        return None

    def invalidate(self, items):
        for f in items:
            if _viewer_has_layer(self.viewer, f):
                layer = self.viewer.layers[f]
                layer.metadata[METADATA_WORKFLOW_VALID_KEY] = False
                self.invalidate(self.workflow.followers_of(f))


    def _register_events_to_viewer(self, viewer: napari.Viewer):
        viewer.dims.events.current_step.connect(self._slider_updated)

        viewer.layers.events.inserted.connect(self._layer_added)
        viewer.layers.events.removed.connect(self._layer_removed)
        viewer.layers.selection.events.changed.connect(self._layer_selection_changed)

    def update(self, target_layer, function, *args, **kwargs):

        def _layer_name_or_value(value, viewer):
            for l in viewer.layers:
                if l.data is value:
                    return l.name
            return value

        args = list(args)
        for i in range(len(args)):
            args[i] = _layer_name_or_value(args[i], self.viewer)
        try:
            if self.viewer in args:
                args.remove(self.viewer)
        except ValueError:
            pass
        args = tuple(args)

        self.workflow.set(target_layer.name, function, *args, **kwargs)

        # set result valid
        target_layer.metadata[METADATA_WORKFLOW_VALID_KEY] = True
        self.invalidate(self.workflow.followers_of(target_layer.name))

    def _register_events_to_layer(self, layer):
        layer.events.data.connect(self._layer_data_updated)

    def _layer_data_updated(self, event):
        print("Layer data updated", event.source, type(event.source))
        event.source.metadata[METADATA_WORKFLOW_VALID_KEY] = True
        for f in self.workflow.followers_of(str(event.source)):
            print("Update", f)
            if _viewer_has_layer(self.viewer, f):
                layer = self.viewer.layers[f]
                self.invalidate(self.workflow.followers_of(f))

    def _layer_added(self, event):
        print("Layer added", event.value, type(event.value))
        self._register_events_to_layer(event.value)

    def _layer_removed(self, event):
        print("Layer removed", event.value, type(event.value))
        self.workflow.remove(event.value.name)

    def _slider_updated(self, event):
        pass
        #print("Slider updated", event.value, type(event.value))

    def _layer_selection_changed(self, event):
        pass
        #print("Layer selection changed", event)

def _viewer_has_layer(viewer, name):
    try:
        layer = viewer.layers[name]
        return layer is not None
    except KeyError:
        return False

def _layer_invalid(layer):
    try:
        return layer.metadata[METADATA_WORKFLOW_VALID_KEY] == False
    except KeyError:
        return False
