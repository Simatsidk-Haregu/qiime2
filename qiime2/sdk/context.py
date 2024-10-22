# ----------------------------------------------------------------------------
# Copyright (c) 2016-2023, QIIME 2 development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

from qiime2.core.type.util import is_collection_type
from qiime2.core.type import HashableInvocation
from qiime2.core.cache import get_cache
import qiime2.sdk
from qiime2.sdk.parallel_config import PARALLEL_CONFIG


class Context:
    def __init__(self, parent=None, parallel=False):
        if parent is not None:
            self.action_executor_mapping = parent.action_executor_mapping
            self.executor_name_type_mapping = parent.executor_name_type_mapping
            self.parallel = parent.parallel
            self.cache = parent.cache
        else:
            self.action_executor_mapping = \
                PARALLEL_CONFIG.action_executor_mapping
            # Cast type to str so yaml doesn't think it needs tpo instantiate
            # an executor object when we write this to then read this from
            # provenance
            self.executor_name_type_mapping = \
                None if PARALLEL_CONFIG.parallel_config is None \
                else {v.label: v.__class__.__name__
                      for v in PARALLEL_CONFIG.parallel_config.executors}
            self.parallel = parallel
            self.cache = get_cache()
            # Only ever do this on the root context. We only want to index the
            # pool once before we start adding our own stuff to it.
            with self.cache.lock:
                if self.cache.named_pool is not None:
                    self.cache.named_pool.create_index()

        self._parent = parent

    def get_action(self, plugin: str, action: str):
        """Return a function matching the callable API of an action.
        This function is aware of the pipeline context and manages its own
        cleanup as appropriate.
        """
        plugin = plugin.replace('_', '-')
        plugin_action = plugin + ':' + action

        pm = qiime2.sdk.PluginManager()
        try:
            plugin_obj = pm.plugins[plugin]
        except KeyError:
            raise ValueError("A plugin named %r could not be found." % plugin)

        try:
            action_obj = plugin_obj.actions[action]
        except KeyError:
            raise ValueError(
                "An action named %r was not found for plugin %r"
                % (action, plugin))

        # We return this callable which determines whether to return cached
        # results or to run the action requested.
        def deferred_action(*args, **kwargs):
            # The function is the first arg, we ditch that
            args = args[1:]

            # If we have a named_pool, we need to check for cached results that
            # we can reuse.
            #
            # We can short circuit our index checking if any of our arguments
            # are proxies because if we got a proxy as an argument, we know it
            # is a new thing we are computing from a prior step in the pipeline
            # and thus will not be cached.
            with self.cache.lock:
                if self.cache.named_pool is not None and \
                        not self._contains_proxies(*args, **kwargs):

                    collated_inputs = action_obj.signature.collate_inputs(
                        *args, **kwargs)
                    callable_args = action_obj.signature.coerce_user_input(
                        **collated_inputs)

                    # Make args and kwargs look how they do when we read them
                    # out of a .yaml file (list of single value dicts of
                    # input_name: value)
                    arguments = []
                    for k, v in callable_args.items():
                        arguments.append({k: v})

                    invocation = HashableInvocation(plugin_action, arguments)
                    if invocation in self.cache.named_pool.index:
                        # It is conceivable that since we created our index the
                        # pool we indexed has been destroyed. If that is the
                        # case we want to just continue on and rerun the action
                        try:
                            return self._load_cache(action_obj, invocation)
                        except KeyError:
                            pass

            # If we didn't have cached results to reuse, we need to execute
            # the action.
            #
            # These factories will create new Contexts with this context as
            # their parent. This allows scope cleanup to happen
            # recursively. A factory is necessary so that independent
            # applications of the returned callable receive their own
            # Context objects.
            #
            # The parsl factory is a bit more complicated because we need
            # to pass this exact Context along for a while longer until we
            # run a normal _bind in action/_run_parsl_action. Then we
            # create a new Context with this one as its parent inside of
            # the parsl app
            def _bind_parsl_context(ctx):
                def _bind_parsl_args(*args, **kwargs):
                    return action_obj._bind_parsl(ctx, *args, **kwargs)
                return _bind_parsl_args

            if self.parallel:
                return _bind_parsl_context(self)(*args, **kwargs)

            return action_obj._bind(
                lambda: Context(parent=self))(*args, **kwargs)

        deferred_action = action_obj._rewrite_wrapper_signature(
            deferred_action)
        action_obj._set_wrapper_properties(deferred_action)
        return deferred_action

    def _load_cache(self, action_obj, invocation):
        """Load cached results
        """
        cached_outputs = self.cache.named_pool.index[invocation]
        loaded_outputs = {}

        for name, _type in action_obj.signature.outputs.items():
            if is_collection_type(_type.qiime_type):
                loaded_collection = qiime2.sdk.ResultCollection()
                cached_collection = cached_outputs[name]

                # Get the order we should load collection items in
                collection_order = list(cached_collection.keys())
                self._validate_collection(collection_order)
                collection_order.sort(key=lambda x: x.idx)

                for elem_info in collection_order:
                    elem = cached_collection[elem_info]
                    loaded_elem = self.cache.named_pool.load(elem)
                    loaded_collection[
                        elem_info.item_name] = loaded_elem

                loaded_outputs[name] = loaded_collection
            else:
                output = cached_outputs[name]
                loaded_outputs[name] = \
                    self.cache.named_pool.load(output)

        return qiime2.sdk.Results(
            loaded_outputs.keys(), loaded_outputs.values())

    def _contains_proxies(self, *args, **kwargs):
        """Returns True if any of the args or kwargs are proxies
        """
        return any(isinstance(arg, qiime2.sdk.proxy.Proxy) for arg in args) \
            or any(isinstance(value, qiime2.sdk.proxy.Proxy) for
                   value in kwargs.values())

    def _validate_collection(self, collection_order):
        """Validate that all indexed items in the collection agree on how
        large the collection should be and that we have that many elements.
        """
        assert all([elem.total == collection_order[0].total
                    for elem in collection_order])
        assert len(collection_order) == collection_order[0].total

    def make_artifact(self, type, view, view_type=None):
        """Return a new artifact from a given view.

        This artifact is automatically tracked and cleaned by the pipeline
        context.
        """
        artifact = qiime2.sdk.Artifact.import_data(type, view, view_type)
        self.add_parent_reference(artifact)
        return artifact

    # NOTE: We end up with both the artifact and the pipeline alias of artifact
    # in the named cache in the end. We only have the pipeline alias in the
    # process pool
    def add_parent_reference(self, ref):
        """Add a reference to something destructable that will be owned by the
           parent scope. The reason it needs to be tracked is so that on
           failure, a context can still identify what will (no longer) be
           returned.
        """
        with self.cache.lock:
            new_ref = self.cache.process_pool.save(ref)

            if self.cache.named_pool is not None:
                self.cache.named_pool.save(new_ref)

        # Return an artifact backed by the data in the cache
        return new_ref
