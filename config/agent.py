"""Configuration for the Scalyr Agent.

For help:
    https://www.scalyr.com/help/scalyr-agent-2
"""

# A "Write Logs" api key for your account, available at
#   https://www.scalyr.com/keys
api_key = "REPLACE_THIS"

# Fields describing this server. These fields are attached to each log
# message, and can be used to filter data from a particular server or
# group of servers.
server_attributes = dict(
    serverHost="specify this field to override the server's hostname",
    tier="production"
)

# Log files to upload to Scalyr. You can use '*' wildcards here.
logs = [
    dict(
        path="/var/log/httpd/access.log",
        attributes=dict(
            parser="accessLog"
        )
    ),
]

monitors = []
