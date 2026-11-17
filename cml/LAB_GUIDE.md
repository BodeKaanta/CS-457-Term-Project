# Sprint 4 Lab Guide: Build, Verify, Capture, Export

A checklist for running the Sprint 4 lab in CML and collecting everything the report needs. Work top to bottom. Each step says what to type, what correct output looks like, and what to check if it doesn't match.

Collect results as you go. Paste text outputs into [`../sprint_4_network_report.md`](../sprint_4_network_report.md) where it says **TODO**, and save screenshots into [`../docs/sprint_4/`](../docs/sprint_4/) with the file names listed there.

| Node (CML label) | Image | Interface | Address |
|---|---|---|---|
| `bk-R1` | IOSv | Gi0/1 / Gi0/2 / Gi0/3 | 192.168.10.1 / 192.168.11.1 / 192.168.20.1 (all /24) |
| `bk-Client1` | Alpine | eth0 → R1 Gi0/1 | DHCP from `CLIENT1_POOL` (expect 192.168.10.2) |
| `bk-Client2` | Alpine | eth0 → R1 Gi0/2 | DHCP from `CLIENT2_POOL` (expect 192.168.11.2) |
| `bk-Server` | Alpine | eth0 → R1 Gi0/3 | static 192.168.20.100/24, gateway 192.168.20.1 |

Alpine console login: `cisco` / `cisco`.

---

## Step 1. Build the topology

**Option A: import (fastest).** In the CML dashboard choose **Import**, select [`pente_lab_import.yaml`](pente_lab_import.yaml), open the lab, and **Start** it. All four nodes, the three links, R1's full configuration, and the server's static address are already in it. IOSv takes a few minutes to boot.

If CML rejects the file (for example because of a schema version difference), use option B. Tell me the error message and I will adjust the file.

**Option B: build by hand.**

1. Add one **IOSv** node and three **Alpine** nodes. Rename them `bk-R1`, `bk-Client1`, `bk-Client2`, `bk-Server`. The custom names are graded.
2. Links: `bk-R1` **Gi0/1** to `bk-Client1` eth0, **Gi0/2** to `bk-Client2` eth0, **Gi0/3** to `bk-Server` eth0. Leave Gi0/0 unused.
3. Start the lab. On R1, type `enable`, then `configure terminal`, and paste the part of [`R1.cfg`](R1.cfg) between the two "cut here" lines. Then type `end` and `write memory`.
4. On `bk-Server`, run the commands in Step 3.

## Step 2. Verify R1

```text
R1# show ip interface brief
R1# show ip route
R1# show hosts
R1# show running-config | section dhcp|dns|host
```

What correct looks like:

- `show ip interface brief`: Gi0/1, Gi0/2, Gi0/3 have their .1 addresses and are **up / up**. Gi0/0 is "administratively down", which is expected.
- `show ip route`: three **C** (connected) routes, for 192.168.10.0/24, 192.168.11.0/24 and 192.168.20.0/24, plus three **L** (local /32) routes. There are no static routes, because none are needed.
- `show hosts`: `server.kaanta.edu` → `192.168.20.100`, type IP, flags `perm`.

If an interface shows **down / down**, the cable isn't connected in CML, or the node at the other end isn't started yet. Gi0/1–0/3 only come **up / up** once the Alpine nodes are running.

> Paste the output of `show ip interface brief` and `show ip route` into report §3. (`show ip dhcp binding` comes after the clients get leases, in Step 4.)

## Step 3. Verify the server's static address (bk-Server)

The imported lab sets these at boot. If you built by hand, or `ip addr` doesn't show them, run:

```sh
sudo ip addr add 192.168.20.100/24 dev eth0
sudo ip link set eth0 up
sudo ip route add default via 192.168.20.1
```

Then check:

```sh
ip addr show eth0      # look for: inet 192.168.20.100/24
ip route               # look for: default via 192.168.20.1 dev eth0
ping -c 3 192.168.20.1 # the server can reach its gateway (R1 Gi0/3)
```

(`RTNETLINK answers: File exists` just means the address or route is already there.)

> Paste `ip addr show eth0` and `ip route` into report §4.

## Step 4. DHCP leases on the clients (bk-Client1, then bk-Client2)

```sh
sudo ip link set eth0 up
sudo udhcpc -i eth0
ip addr show eth0
ip route
cat /etc/resolv.conf
```

What correct looks like (Client 1; Client 2 is the same with `11` in place of `10`):

```text
udhcpc: broadcasting discover
udhcpc: broadcasting select for 192.168.10.2, server 192.168.10.1
udhcpc: lease of 192.168.10.2 obtained from 192.168.10.1, lease time 86400
...
    inet 192.168.10.2/24 ...
default via 192.168.10.1 dev eth0
nameserver 192.168.10.1
```

Those three lines show the three things DHCP delivered: the **address** (from the pool), the **default gateway** (`default-router`, option 3), and the **DNS server** (`dns-server`, option 6).

Then on R1:

```text
R1# show ip dhcp binding
```

It should list 192.168.10.2 on Gi0/1 and 192.168.11.2 on Gi0/2, both Automatic / Active.

If `udhcpc` keeps saying "broadcasting discover" with no lease:
- Check R1 Gi0/1 (or Gi0/2) is up/up.
- `show ip dhcp pool` should show the pool's subnet matching the interface.
- `show ip dhcp conflict` lists addresses R1 refuses to lease.
- `debug ip dhcp server events` on R1 shows what the server is doing (`undebug all` to stop).

> Paste the client outputs into report §5 and `show ip dhcp binding` into §3.

## Step 5. Ping the game server from both clients

```sh
ping -c 4 192.168.20.100
```

Expect `4 packets transmitted, 4 packets received, 0% packet loss`. The first reply can be slow while ARP resolves; that is normal. If the very first ping times out, run it again.

If every ping fails:
- `ping 192.168.10.1` (the client's own gateway) works but the server doesn't answer: check the server's **default route** (Step 3). Without it, the server receives the ping but can't send the reply back to another subnet.
- `ping 192.168.20.1` from the client works but `.100` doesn't: check the server's address and that the R1–Server link is up.

> Paste both clients' ping output into report §6.

## Step 6. DNS lookup, with the Wireshark capture running

1. **Start the capture first.** In CML, select the link between `bk-R1` and `bk-Client1`, open its **Packet Capture** panel (in some CML versions: right-click the link → *Start capture*), and start capturing. A filter of `udp port 53` keeps the capture small but is optional.
2. On `bk-Client1`:

   ```sh
   nslookup server.kaanta.edu
   ```

   Expected (BusyBox `nslookup`):

   ```text
   Server:    192.168.10.1
   Address:   192.168.10.1:53

   Name:      server.kaanta.edu
   Address:   192.168.20.100

   *** Can't find server.kaanta.edu: No answer
   ```

   The last line is **expected**. BusyBox `nslookup` also asks for an IPv6 (AAAA) address, and R1 has none, so that second lookup is empty. The IPv4 answer above it is the one that matters.

   If the output also says **"Non-authoritative answer:"**, R1 is missing `ip dns primary kaanta.edu soa R1.kaanta.edu admin.kaanta.edu` (in [`R1.cfg`](R1.cfg)). Without it, IOS answers from the host table without the authoritative (AA) flag. Add it in `configure terminal`, then `write memory`, then run `nslookup` again.

3. **Stop** the capture and **download** it. Save it in the repository root as **`dns_raw.pcap`** (or `dns_raw.pcapng`, whichever CML gives you). This is the raw capture; Step 5 turns it into the deliverable. `dns_raw.*` is in `.gitignore`. **Check where your browser saved it.** It must end up in the `CS-457-Term-Project-sprint4` folder (move it there if needed), and Step 5 must be run from that folder.
4. Run `nslookup server.kaanta.edu` on `bk-Client2` as well, for the report. No capture is needed for this one.
5. On this PC, check the capture and write the **annotated** deliverable:

   ```bash
   python tools/dns_pcap_report.py dns_raw.pcap --annotate sprint_4_dns_lookup.pcap
   ```

   This does two things. It prints a table of every DNS packet and a written explanation of the query and the response, ending with `PASS` if the capture shows `server.kaanta.edu` answered with `192.168.20.100`; paste that output into report §8. It also writes **`sprint_4_dns_lookup.pcap`**: the same packets, byte for byte with the same timestamps, plus a **Wireshark packet comment on every DNS packet** and a comment on the capture file. The rubric gives only partial credit for a capture "missing annotations", so this file is the one to commit.

   The file is in **pcapng** format even though the required name ends in `.pcap`, because the classic pcap format cannot store comments. Wireshark identifies the format from the file's contents, not its name, so it opens normally and shows the comments.
6. **Annotated Wireshark screenshot:** open `sprint_4_dns_lookup.pcap` in Wireshark and apply the display filter `dns`. Click the **response** packet. In the details pane, expand **Packet comments** (at the top) and *Domain Name System (response) → Answers*, so both the comment and `server.kaanta.edu: type A, class IN, addr 192.168.20.100` are visible. Save the screenshot as `docs/sprint_4/wireshark_dns.png`. For extra clarity, you can draw labels on it in Snipping Tool (e.g. "query", "response", "answer 192.168.20.100"). Optionally, also save `docs/sprint_4/wireshark_dns_query.png` with the **query** packet selected and its comment expanded.

If `nslookup` fails:
- `cat /etc/resolv.conf` must say `nameserver 192.168.10.1`. If not, run `udhcpc` again.
- On R1, `show hosts` must list the name.
- `show running-config | include dns|domain` must show `ip dns server` and `ip dns primary kaanta.edu ...`, and must **not** show `no ip domain lookup`.

> To see every comment at once in Wireshark: *Statistics → Capture File Properties* lists the capture comment and all packet comments. You can also add or edit a comment by hand: right-click a packet → *Packet Comments*, then save in pcapng format.

## Step 7. Topology screenshot

Arrange the canvas so all four nodes, their custom names, and the links are readable. Turning on interface labels helps. Save the screenshot as `docs/sprint_4/topology.png`.

## Step 8. Save and export the topology

1. On R1: `write memory`.
2. In CML, **extract the configurations** so the export contains what is actually running. In the lab's *Nodes* menu choose *Extract configurations*, or use the per-node *Extract* button in each node's *Config* tab.
3. **Export / Download** the lab as YAML and save it in the repository root as **`sprint_4_topology.yaml`**.

## Step 9. Commit

Back on this PC, from the `CS-457-Term-Project-sprint4` folder:

```bash
git add sprint_4_topology.yaml sprint_4_dns_lookup.pcap sprint_4_network_report.md docs/sprint_4/
git commit -m "Sprint 4: lab results"
```

Then push and submit the repository URL on Canvas.
