"""The privileged half of systemd actuation, and the wire it speaks.

eidolond runs as the unprivileged `eidolon` user and must still start, stop and
restart twelve product units. Route taken here: a root-side applier reached over
a Unix socket, which authorises a request by resolving the *caller's own systemd
unit* from its cgroup. That is the same fact polkit's `subject.system_unit`
carries, decided in code we own and log, rather than in a rule engine that on
this board wrote nothing to the journal when it refused.

`protocol` is shared with the client adapter. `authorize` and `server` are
root-only and the import contract forbids the manager from reaching into them.
"""
