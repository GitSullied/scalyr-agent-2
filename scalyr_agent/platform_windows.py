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

try:
    from scalyr_agent.platform_controller import PlatformController, DefaultPaths
except ImportError:
    # The module lookup path list might fail when this module is being hosted by
    # PythonService.exe, so append the lookup path and try again.
    from os import path
    sys.path.append(
        path.dirname(
            path.dirname(
                path.abspath(__file__)
            )
        )
    )
    from scalyr_agent.platform_controller import PlatformController, DefaultPaths

from __scalyr__ import get_install_root

import win32serviceutil
import win32service
import servicemanager
import win32event
import win32api


class ScalyrService(win32serviceutil.ServiceFramework):
    _svc_name_ = "ScalyrAgent"
    _svc_display_name_ = "Scalyr Agent"
    _svc_description_ = "Hosts Scalyr metric collectors"

    def __init__(self, *args):
        win32serviceutil.ServiceFramework.__init__(self, *args)
        self.log('Instantiate scalyr service')
        self._stop_event = win32event.CreateEvent(None, 0, 0, None)

    def log(self, msg):
        servicemanager.LogInfoMsg(msg)

    def sleep(self, sec):
        win32api.Sleep(sec*1000, True)

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        self.log('Stopping scalyr service')
        #self.running = False
        win32event.SetEvent(self._stop_event)
        self.log('Stopped scalyr service')
        self.ReportServiceStatus(win32service.SERVICE_STOPPED)

    def SvcDoRun(self):
        self.ReportServiceStatus(win32service.SERVICE_START_PENDING)
        try:
            self.ReportServiceStatus(win32service.SERVICE_RUNNING)
            self.log('Starting service')
            #self.start()
            self.log('Waiting for stop event')
            win32event.WaitForSingleObject(self._stop_event, win32event.INFINITE)
            self.log('Done waiting')
        except Exception, e:
            self.log('ERROR: {}'.format(e))
            self.SvcStop()

    #def start(self):
    #    self.running = True
    #    while self.running:
    #        self.sleep(10)
    #        self.log('Scalyr Service runloop...')


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

    def run_as_user(self, user_id, script_file, script_arguments):
        """Restarts this process with the same arguments as the specified user.

        This will re-run the entire Python script so that it is executing as the specified user.
        It will also add in the '--no-change-user' option which can be used by the script being executed with the
        next proces that it was the result of restart so that it probably shouldn't do that again.

        @param user_id: The user id to run as, typically 0 for root.
        @param script_file: The path to the Python script file that was executed.
        @param script_arguments: The arguments passed in on the command line that need to be used for the new
            command line.

        @type user_id: int
        @type script_file: str
        @type script_arguments: list<str>
        """
        print "** run_as_user **"
        print "user_id", user_id
        print "script_file", script_file
        print "script_arguments", script_arguments
        print "**** run_as_user **"

    def is_agent_running(self, fail_if_running=False):
        """Returns true if the agent service is running, as determined by this platform implementation.

        This will optionally raise an Exception with an appropriate error message if the agent is not running.

        @param fail_if_running:  True if the method should raise an Exception with a platform-specific error message
            explaining how it determined the agent is not running.
        @type fail_if_running: bool

        @return: True if the agent process is already running.
        @rtype: bool

        @raise AgentAlreadyRunning: If the agent is running and fail_if_running is True.
        """
        print "** is_agent_running **"
        print "fail_if_running", fail_if_running
        print "**** is_agent_running **"

    def start_agent_service(self, agent_run_method, quiet):
        """Start the agent service using the platform-specific method.

        This method must return once the agent service has been started.

        @param agent_run_method: The method to invoke to actually run the agent service.  This method takes one
            argument, the reference to this controller.  Note, if your platform implementation cannot use this
            function pointer (because the service is running in a separate address space and cannot be passed this
            pointer), then instead of invoking this method, you may invoke ScalyrAgent.agent_run_method instead.
        @param quiet: True if only error messages should be printed to stdout, stderr.

        @type agent_run_method: func(PlatformController)
        @type quiet: bool
        """
        print "** start_agent_service **"
        print "agent_run_method", agent_run_method
        print "quiet", quiet
        print "**** is_agent_running **"

    def stop_agent_service(self, quiet):
        """Stops the agent service using the platform-specific method.

        This method must return only after the agent service has terminated.

        @param quiet: True if only error messages should be printed to stdout, stderr.
        @type quiet: bool
        """
        print "** stop_agent_service **"
        print "quiet", quiet
        print "**** stop_agent_service **"


if __name__ == "__main__":
    sys.exit(
        win32serviceutil.HandleCommandLine(ScalyrService)
    )
