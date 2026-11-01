# Security Policy

JoustMania is a party game that runs on a local network (a Raspberry Pi or a laptop next to the play area). It is a
demo / hobby project, not a hosted service.

## Supported versions

Only the latest release receives security fixes. Please check that the problem still exists on the latest release
(or on `main`) before reporting it.

## Reporting a vulnerability

Please do not open a public issue for a security problem.

Report it privately through GitHub Security Advisories:
<https://github.com/WatchMeJoustMyFlags/JoustMania/security/advisories/new>

Include what you found, the version or commit, and steps to reproduce it. This is a volunteer project: we aim to
acknowledge a report within about a week, but we give no fixed fix timeline.

## Scope

The stage setup runs on a trusted local network. Some endpoints are intentionally unauthenticated for the demo (for
example the agent `/experiments` write endpoints, tracked in
[#2547](https://github.com/WatchMeJoustMyFlags/JoustMania/issues/2547)). Hardening them is planned for later and is
not treated as an undisclosed vulnerability. The services are not meant to be exposed to the internet.

In scope:
- code execution, privilege escalation or file access beyond what a service is meant to do (including the
  Pi helpers that run as root)
- leaking secrets such as API keys, through the code, the images or the CI workflows
- supply-chain problems in our images, dependencies or CI / release pipeline
- bugs that let a player or a network peer outside the intended trust model take over a service

Out of scope:
- missing authentication on the demo endpoints named above, when the stack is on a local network
- running the stack exposed to the public internet against the documentation
- denial of service by someone who already has access to the local network or the controllers
- vulnerabilities in third-party software that are already fixed upstream and only need a dependency bump
  (report those as a normal issue)
