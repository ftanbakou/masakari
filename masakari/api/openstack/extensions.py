# Copyright (c) 2016 NTT DATA
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import abc
import functools
import os

from oslo_log import log as logging
from oslo_utils import importutils
import webob.dec
import webob.exc

from masakari.api.openstack import wsgi
from masakari import exception
from masakari.i18n import _

LOG = logging.getLogger(__name__)


class ExtensionDescriptor(object):
    """Base class that defines the contract for extensions.

    Note that you don't have to derive from this class to have a valid
    extension; it is purely a convenience.

    """

    # The name of the extension, e.g., 'Fox In Socks'
    name = None

    # The alias for the extension, e.g., 'FOXNSOX'
    alias = None

    # Description comes from the docstring for the class

    # The timestamp when the extension was last updated, e.g.,
    # '2011-01-22T19:25:27Z'
    updated = None

    def __init__(self, ext_mgr):
        """Register extension with the extension manager."""

        ext_mgr.register(self)
        self.ext_mgr = ext_mgr

    def get_resources(self):
        """List of extensions.ResourceExtension extension objects.

        Resources define new nouns, and are accessible through URLs.

        """
        resources = []
        return resources

    def get_controller_extensions(self):
        """List of extensions.ControllerExtension extension objects.

        Controller extensions are used to extend existing controllers.
        """
        controller_exts = []
        return controller_exts

    def __repr__(self):
        return "<Extension: name=%s, alias=%s, updated=%s>" % (
            self.name, self.alias, self.updated)

    def is_valid(self):
        """Validate required fields for extensions.

        Raises an attribute error if the attr is not defined
        """
        for attr in ('name', 'alias', 'updated', 'namespace'):
            if getattr(self, attr) is None:
                raise AttributeError("%s is None, needs to be defined" % attr)
        return True


class ExtensionsController(wsgi.Resource):

    def __init__(self, extension_manager):
        self.extension_manager = extension_manager
        super(ExtensionsController, self).__init__(None)

    def _translate(self, ext):
        ext_data = {'name': ext.name, 'alias': ext.alias,
                    'description': ext.__doc__, 'namespace': ext.namespace,
                    'updated': ext.updated, 'links': []}
        return ext_data

    def index(self, req):
        extensions = []
        for ext in self.extension_manager.sorted_extensions():
            extensions.append(self._translate(ext))
        return dict(extensions=extensions)

    def show(self, req, id):
        try:
            ext = self.extension_manager.extensions[id]
        except KeyError:
            raise webob.exc.HTTPNotFound()

        return dict(extension=self._translate(ext))

    def delete(self, req, id):
        raise webob.exc.HTTPNotFound()

    def create(self, req, body):
        raise webob.exc.HTTPNotFound()


class ExtensionManager(object):
    """Load extensions from the configured extension path."""

    def sorted_extensions(self):
        if self.sorted_ext_list is None:
            self.sorted_ext_list = sorted(self.extensions.items())

        for _alias, ext in self.sorted_ext_list:
            yield ext

    def is_loaded(self, alias):
        return alias in self.extensions

    def register(self, ext):
        # Do nothing if the extension doesn't check out
        if not self._check_extension(ext):
            return

        alias = ext.alias
        if alias in self.extensions:
            raise exception.MasakariException(
                "Found duplicate extension: %s" % alias)
        self.extensions[alias] = ext
        self.sorted_ext_list = None

    def get_resources(self):
        """Returns a list of ResourceExtension objects."""

        resources = []
        resources.append(ResourceExtension('extensions',
                                           ExtensionsController(self)))
        for ext in self.sorted_extensions():
            try:
                resources.extend(ext.get_resources())
            except AttributeError:
                pass
        return resources

    def get_controller_extensions(self):
        """Returns a list of ControllerExtension objects."""
        controller_exts = []
        for ext in self.sorted_extensions():
            try:
                get_ext_method = ext.get_controller_extensions
            except AttributeError:
                continue
            controller_exts.extend(get_ext_method())
        return controller_exts

    def _check_extension(self, extension):
        """Checks for required methods in extension objects."""
        try:
            extension.is_valid()
        except AttributeError:
            LOG.exception("Exception loading extension")
            return False

        return True

    def load_extension(self, ext_factory):
        """Execute an extension factory.

        Loads an extension.  The 'ext_factory' is the name of a
        callable that will be imported and called with one
        argument--the extension manager.  The factory callable is
        expected to call the register() method at least once.
        """

        LOG.debug("Loading extension %s", ext_factory)

        if isinstance(ext_factory, str):
            # Load the factory
            factory = importutils.import_class(ext_factory)
        else:
            factory = ext_factory

        # Call it
        LOG.debug("Calling extension factory %s", ext_factory)
        factory(self)

    def _load_extensions(self):
        """Load extensions specified on the command line."""

        extensions = list(self.cls_list)

        for ext_factory in extensions:
            try:
                self.load_extension(ext_factory)
            except Exception as exc:
                LOG.warning('Failed to load extension %(ext_factory)s: '
                            '%(exc)s',
                            {'ext_factory': ext_factory, 'exc': exc})


class ControllerExtension(object):
    """Extend core controllers of masakari OpenStack API.

    Provide a way to extend existing masakari OpenStack API core
    controllers.
    """

    def __init__(self, extension, collection, controller):
        self.extension = extension
        self.collection = collection
        self.controller = controller


class ResourceExtension(object):
    """Add top level resources to the OpenStack API in masakari."""

    def __init__(self, collection, controller=None, parent=None,
                 collection_actions=None, member_actions=None,
                 custom_routes_fn=None, inherits=None, member_name=None):
        if not collection_actions:
            collection_actions = {}
        if not member_actions:
            member_actions = {}
        self.collection = collection
        self.controller = controller
        self.parent = parent
        self.collection_actions = collection_actions
        self.member_actions = member_actions
        self.custom_routes_fn = custom_routes_fn
        self.inherits = inherits
        self.member_name = member_name


def load_standard_extensions(ext_mgr, logger, path, package, ext_list=None):
    """Registers all standard API extensions."""

    # Walk through all the modules in our directory...
    our_dir = path[0]
    for dirpath, dirnames, filenames in os.walk(our_dir):
        # Compute the relative package name from the dirpath
        relpath = os.path.relpath(dirpath, our_dir)
        if relpath == '.':
            relpkg = ''
        else:
            relpkg = '.%s' % '.'.join(relpath.split(os.sep))

        # Now, consider each file in turn, only considering .py files
        for fname in filenames:
            root, ext = os.path.splitext(fname)

            # Skip __init__ and anything that's not .py
            if ext != '.py' or root == '__init__':
                continue

            # Try loading it
            classname = "%s%s" % (root[0].upper(), root[1:])
            classpath = ("%s%s.%s.%s" %
                         (package, relpkg, root, classname))

            if ext_list is not None and classname not in ext_list:
                logger.debug("Skipping extension: %s" % classpath)
                continue

            try:
                ext_mgr.load_extension(classpath)
            except Exception as exc:
                logger.warn('Failed to load extension %(classpath)s: %(exc)s',
                            {'classpath': classpath, 'exc': exc})

        # Now, let's consider any subdirectories we may have...
        subdirs = []
        for dname in dirnames:
            # Skip it if it does not have __init__.py
            if not os.path.exists(os.path.join(dirpath, dname, '__init__.py')):
                continue

            # If it has extension(), delegate...
            ext_name = "%s%s.%s.extension" % (package, relpkg, dname)
            try:
                ext = importutils.import_class(ext_name)
            except ImportError:
                # extension() doesn't exist on it, so we'll explore
                # the directory for ourselves
                subdirs.append(dname)
            else:
                try:
                    ext(ext_mgr)
                except Exception as exc:
                    logger.warn('Failed to load extension %(ext_name)s:'
                                '%(exc)s',
                                {'ext_name': ext_name, 'exc': exc})

        # Update the list of directories we'll explore...
        # using os.walk 'the caller can modify the dirnames list in-place,
        # and walk() will only recurse into the subdirectories whose names
        # remain in dirnames'
        # https://docs.python.org/2/library/os.html#os.walk
        dirnames[:] = subdirs


class V1APIExtensionBase(object, metaclass=abc.ABCMeta):
    """Abstract base class for all v1 API extensions.

    All v1 API extensions must derive from this class and implement
    the abstract methods get_resources and get_controller_extensions
    even if they just return an empty list. The extensions must also
    define the abstract properties.
    """

    def __init__(self, extension_info):
        self.extension_info = extension_info

    @abc.abstractmethod
    def get_resources(self):
        """Return a list of resources extensions.

        The extensions should return a list of ResourceExtension
        objects. This list may be empty.
        """
        pass

    @abc.abstractmethod
    def get_controller_extensions(self):
        """Return a list of controller extensions.

        The extensions should return a list of ControllerExtension
        objects. This list may be empty.
        """
        pass

    @property
    @abc.abstractmethod
    def name(self):
        """Name of the extension."""
        pass

    @property
    @abc.abstractmethod
    def alias(self):
        """Alias for the extension."""
        pass

    @property
    @abc.abstractmethod
    def version(self):
        """Version of the extension."""
        pass

    def __repr__(self):
        return "<Extension: name=%s, alias=%s, version=%s>" % (
            self.name, self.alias, self.version)

    def is_valid(self):
        """Validate required fields for extensions.

        Raises an attribute error if the attr is not defined
        """
        for attr in ('name', 'alias', 'version'):
            if getattr(self, attr) is None:
                raise AttributeError("%s is None, needs to be defined" % attr)
        return True


def expected_errors(errors):
    """Decorator for v1 API methods which specifies expected exceptions.

    Specify which exceptions may occur when an API method is called. If an
    unexpected exception occurs then return a 500 instead and ask the user
    of the API to file a bug report.
    """
    def decorator(f):
        @functools.wraps(f)
        def wrapped(*args, **kwargs):
            try:
                return f(*args, **kwargs)
            except Exception as exc:
                if isinstance(exc, webob.exc.WSGIHTTPException):
                    if isinstance(errors, int):
                        t_errors = (errors,)
                    else:
                        t_errors = errors
                    if exc.code in t_errors:
                        raise
                elif isinstance(exc, exception.Forbidden):
                    raise
                elif isinstance(exc, exception.ValidationError):
                    raise
                LOG.exception("Unexpected exception in API method")
                msg = _('Unexpected API Error. Please report this at '
                        'https://bugs.launchpad.net/masakari/ and attach the '
                        'Masakari API log if possible.\n%s') % type(exc)
                raise webob.exc.HTTPInternalServerError(explanation=msg)

        return wrapped

    return decorator
