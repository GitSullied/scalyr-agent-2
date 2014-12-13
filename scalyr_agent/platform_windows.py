# Copyright 2014 Scalyr Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ------------------------------------------------------------------------
#
# author: Scott Sullivan <guy.hoozdis@gmail.com>

__author__ = 'guy.hoozdis@gmail.com'

import sys

from scalyr_agent.platform_controller import PlatformController, DefaultPaths

from __scalyr__ import get_install_root


class WindowsPlatformController(PlatformController):
    """A controller instance for Microsoft's Windows platforms
    """

    def can_handle_current_platform(self):
        """Returns true if this platform object can handle the server this process is running on.

        @return: True if this platform instance can handle the current server.
        @rtype: bool
        """
        return 'win32' == sys.platform

    @property
    def default_paths(self):
        """Returns the default paths to use for various configuration options for this platform.

        @return: The default paths
        @rtype: DefaultPaths
        """
        # NOTE: For this module, it is assumed that the 'install_type' is always PACKAGE_INSTALL
        # TODO: These are not ideal paths, just something to get us started.
        return DefaultPaths(
                r'\Temp\scalyr\log',
                r'\Temp\scalyr\agent.json',
                r'\Temp\scalyr\lib')

