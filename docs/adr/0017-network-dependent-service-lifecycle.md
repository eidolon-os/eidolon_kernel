# ADR 0017: Network inputs belong to Host service lifecycle

Status: Implemented; deployment and physical Wi-Fi relocation acceptance pending.

## Problem

A Host identity survives DHCP renewal and network relocation. Its reachable
addresses do not. Mac and Linux LiveKit launchers previously detected one LAN
address at process start and wrote it as `rtc.node_ip`. On the affected Mac,
the generated configuration still contained `192.168.100.18` while the Host
answered on `192.168.1.32`. HTTP discovery could succeed while RTC failed.

Simply removing the override is insufficient for the installed LiveKit 1.11.0:
its transport constructs one Pion `stdnet.Net`, whose interface inventory is
captured at creation. Native continual gathering is not configured by this
LiveKit transport. A process must be refreshed after its network inputs change.

Primary implementation references:

- [LiveKit transport configuration](https://github.com/livekit/mediatransportutil/blob/0fcb3771c3d5/pkg/rtcconfig/webrtc_config.go)
- [Pion interface inventory](https://github.com/pion/transport/blob/v4.0.1/stdnet/net.go)

## Address responsibilities

| Value | Lifetime and authority | Handling |
| --- | --- | --- |
| Host, Device, Owner, Companion identity | Stable authority identity | Never replace or re-enrol because an IP changed |
| Same-Host NATS, HTTP and gRPC connections | Local service topology | Use loopback or Unix sockets; no LAN rewriting |
| Listener bind address | Service policy | Use wildcard where LAN access is intended; it is not a client destination |
| Discovered Host/Owner routes | Current network observation | Existing discovery/resolution, identity verification and invalidation |
| Device-facing LiveKit signalling URL | Channel binding | Reuse Channel's current per-binding resolution; explicit remote URLs stay explicit |
| Local RTC candidates | LiveKit transport instance | Native ICE gathering; refresh the instance after network changes |
| Public DNS / NAT mapping / deliberately fixed address | Operator deployment policy | Preserve explicit overrides; never infer such policy from an observed DHCP address |

No Mobile-only route, token claim, companion resolver, enrolment operation or
Room protocol is added. Host discovery still establishes management access;
media has its own readiness within the existing service directory.

Operation submission/completion is now governed by [ADR 0019](0019-observed-service-operation-convergence.md).

## Decision

1. Mac and Linux launchers stop injecting automatically detected `node_ip`.
   `EIDOLON_LIVEKIT_NODE_IP`, when explicitly set, remains an IP-valued operator
   override. Native ICE can offer multiple eligible interfaces. Example Host
   profiles default to dynamic observation instead of an enabled literal IP.
2. The shared service manifest declares `restart_on_network_change` only on
   services whose transport captures network inputs. LiveKit opts in; NATS does
   not. This policy is independent of the `systemd` and `supervisord` drivers.
3. The existing eidolond reconciliation loop samples the OS through psutil,
   already used for Host telemetry. It observes active non-loopback/non-link-local
   IPs, netmasks and default-route source addresses. Route lookup uses a UDP
   socket without sending traffic. Linux grants the same AF_NETLINK read surface
   as the existing Hub network observer while retaining an empty capability set.
   There is no new daemon or discovery protocol.
4. An input must remain unchanged for 10 seconds before use. A missing or
   unsettled network withholds the affected service's ready endpoints without
   repeatedly restarting it. Changed inputs refresh only opted-in managed
   services. Failed refreshes retry no sooner than 30 seconds.
5. Applied observations are an atomic private cache next to the eidolond DB,
   separate from desired state and enrolment data. Each receipt combines the
   network fingerprint and actual process instance (systemd InvocationID or
   supervisord PID plus process creation time). Missing/corrupt observations and
   externally replaced processes require adoption through one refresh. Restarting
   eidolond alone does not restart an already reconciled process.
6. Re-read network input after a successful process operation before recording
   it. Changes during start/restart and observation-write failures cannot publish
   ready endpoints. Disabled and external services keep their existing ownership
   semantics. Explicit manual restarts also update the observation.
7. Ops asks eidolond for LiveKit's current service state through the existing
   local service API. The service explicitly reports `network_current`; an old
   daemon or manifest returning only ordinary `ready` cannot satisfy the check.
   The readiness fact is `livekit_network_current`; it no
   longer treats a YAML `node_ip` as proof of active media readiness. Native HTTP
   health is checked too. Actual external media remains an acceptance gate.

## Validation and limits

Verified on 2026-09-10: Kernel full suite 463 passed / 1 skipped; after the final
readiness fact and Linux sandbox checks, the system suite passed 121 / 1 skipped.
Ops full suite passed 1068; Channel LiveKit suite passed 52; Admin service client
suite passed 13. On Opi5max the exact network observation source also returned a
valid fingerprint inside a transient systemd process running as `eidolon`, with
NoNewPrivileges, an empty capability set and the declared address-family allowlist.
No existing Opi5max service or deployment was changed. The isolated Mac TCP test
observed old-candidate timeout, actual process replacement, successful RTC data
delivery, and no restart on the following unchanged reconciliation. Test evidence
was retained at `/tmp/eid-network-vf5sqevr` (temporary local artifact).

Regression coverage includes DHCP-like changes, multiple interfaces, link-local
filtering, no network, flapping, cooldown after failure, network changes during
restart, cache failure/corruption, daemon restarts, process replacement, manual
restart, external ownership, and native/explicit policies on both launchers.

The native LiveKit binary and SDK are tested separately on Mac. A freshly started
instance with a stale override can still connect over UDP via other ICE behavior;
that is not a reproduction of an old process's cached interfaces. A TCP-only
candidate test isolates the stale advertised address behavior. Simulated network
inputs test real supervisor process replacement; this does not claim a physical
Wi-Fi move occurred.

This change refreshes service transport state; it does not promise preservation
of an in-flight voice turn across a network interruption. Existing clients retain
responsibility for reconnection and obtaining a current session binding. A
multi-homed Host still follows the existing signalling route policy; no claim is
made that a default route is reachable from every possible client subnet.

Deploy the Kernel and Ops changes together through the normal release/source
activation flow. Existing generated manifests must be regenerated from the new
source; restarting an old manifest alone does not enable this policy. The first
adoption of an already running opted-in process requires one refresh. Physical
acceptance: relocate Mac and Linux Hosts without restarting them manually,
rediscover the same Host identity, enter a Room again, and verify audio plus
current ICE candidates; verify NATS and enrolled devices retain their identities.
