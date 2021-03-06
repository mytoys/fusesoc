import argparse
from collections import OrderedDict
import copy
import os
import shutil
import sys
import logging

if sys.version_info[0] >= 3:
    import urllib.request as urllib
    from urllib.error import URLError
    from urllib.error import HTTPError
else:
    import urllib
    from urllib2 import URLError
    from urllib2 import HTTPError

from fusesoc.config import Config
from fusesoc.coremanager import CoreManager

logger = logging.getLogger(__name__)

class FileAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        path = os.path.expandvars(values[0])
        path = os.path.expanduser(path)
        path = os.path.abspath(path)
        setattr(namespace, self.dest, [path])

class EdaTool(object):

    def __init__(self, system, export, toplevel):
        self.system = system
        self.export = export
        self.TOOL_NAME = self.__class__.__name__.lower()
        self.flags = {'tool'   : self.TOOL_NAME,
                      'flow'   : self.TOOL_TYPE}
        build_root = os.path.join(Config().build_root, self.system.sanitized_name)

        self.src_root  = os.path.join(build_root, 'src')
        self.work_root = os.path.join(build_root, self.TOOL_TYPE+'-'+self.TOOL_NAME)
        self.cm = CoreManager()
        self.cores = self.cm.get_depends(self.system.name,
                                         self.flags)

        self.env = os.environ.copy()

        #FIXME: Remove BUILD_ROOT once cores have had some time
        # to migrate to SRC_ROOT/WORK_ROOT
        self.env['BUILD_ROOT'] = os.path.abspath(build_root)
        self.env['SRC_ROOT']  = os.path.abspath(self.src_root)
        self.env['WORK_ROOT'] = os.path.abspath(self.work_root)

        self.plusarg     = OrderedDict()
        self.vlogparam   = OrderedDict()
        self.vlogdefine  = OrderedDict()
        self.generic     = OrderedDict()
        self.cmdlinearg  = OrderedDict()
        self.parsed_args = False

        self.toplevel = toplevel

    def configure(self, args):
        if os.path.exists(self.work_root):
            for f in os.listdir(self.work_root):
                if os.path.isdir(os.path.join(self.work_root, f)):
                    shutil.rmtree(os.path.join(self.work_root, f))
                else:
                    os.remove(os.path.join(self.work_root, f))
        else:
            os.makedirs(self.work_root)

        _flags = self.flags.copy()
        for core in self.cores:
            logger.info("Preparing " + str(core.name))
            dst_dir = os.path.join(self.src_root, core.sanitized_name)
            try:
                core.setup()
            except URLError as e:
                raise RuntimeError("Problem while fetching '" + core.name + "': " + str(e.reason))
            except HTTPError as e:
                raise RuntimeError("Problem while fetching '" + core.name + "': " + str(e.reason))

            if self.export:
                _flags['is_toplevel'] = (core.name == self.system.name)
                core.export(dst_dir, _flags)

    def parse_args(self, args, prog, paramtypes):
        if self.parsed_args:
            return
        typedict = {'bool' : {'action' : 'store_true'},
                    'file' : {'type' : str , 'nargs' : 1, 'action' : FileAction},
                    'int'  : {'type' : int , 'nargs' : 1},
                    'str'  : {'type' : str , 'nargs' : 1},
                    }
        progname = 'fusesoc {} {}'.format(prog,
                                          self.system.name)
        parser = argparse.ArgumentParser(prog = progname,
                                         conflict_handler='resolve')
        param_groups = {}
        _descr = {'plusarg'    : 'Verilog plusargs (Run-time option)',
                  'vlogparam'  : 'Verilog parameters (Compile-time option)',
                  'vlogdefine' : 'Verilog defines (Compile-time global symbol)',
                  'generic'    : 'VHDL generic (Run-time option)',
                  'cmdlinearg' : 'Command-line arguments (Run-time option)'}
        all_params = {}
        _flags = self.flags.copy()
        for core in self.cores:
            _flags['is_toplevel'] = (core.name == self.system.name)
            for param in core.get_parameters(_flags):
                if param.paramtype in paramtypes:
                    if not param.paramtype in param_groups:
                        param_groups[param.paramtype] = \
                        parser.add_argument_group(_descr[param.paramtype])

                    default = None
                    if not param.default == '':
                        try:
                            default = [typedict[param.datatype]['type'](param.default)]
                        except KeyError as e:
                            pass
                    try:
                        param_groups[param.paramtype].add_argument('--'+param.name,
                                                                   help=param.description,
                                                                   default=default,
                                                                   **typedict[param.datatype])
                    except KeyError as e:
                        raise RuntimeError("Invalid data type {} for parameter '{}' in '{}'".format(str(e),
                                                                                                   param.name,
                                                                                                   core.name))
                    all_params[param.name.replace('-','_')] = param.paramtype
        p = parser.parse_args(args)

        for key,value in sorted(vars(p).items()):
            paramtype = all_params[key]
            if value is None:
                continue

            if type(value) == bool:
                _value = value
            else:
                _value = value[0]

            getattr(self, paramtype)[key] = _value
        self.parsed_args = True

    def _get_fileset_files(self):
        incdirs = []
        src_files = []
        _flags = self.flags.copy()
        for core in self.cores:
            if self.export:
                files_root = os.path.join(self.src_root, core.sanitized_name)
            else:
                files_root = core.files_root
            basepath = os.path.relpath(files_root, self.work_root)
            _flags['is_toplevel'] = (core.name == self.system.name)
            for file in core.get_files(_flags):
                if file.is_include_file:
                    _incdir = os.path.join(basepath, os.path.dirname(file.name))
                    if not _incdir in incdirs:
                        incdirs.append(_incdir)
                else:
                    new_file = copy.deepcopy(file)
                    new_file.name = os.path.join(basepath, file.name)
                    src_files.append(new_file)

        return (src_files, incdirs)

    """ Convert a parameter value to string suitable to be passed to an EDA tool

    Rules:
    - Booleans are represented as 0/1
    - Strings are either passed through (strings_in_quotes=False) or
      put into double quotation marks (")
    - Everything else (including int, float, etc.) are converted using the str()
      function.
    """
    def _param_value_str(self, param_value, strings_in_quotes=False):

      if type(param_value) == bool:
          if (param_value) == True:
              return '1'
          else:
              return '0'
      elif type(param_value) == str:
          if strings_in_quotes:
              return '"'+str(param_value)+'"'
          else:
              return str(param_value)
      else:
          return str(param_value)
