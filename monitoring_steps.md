# Infrastructure monitoring with Prometheus and Grafana

These steps add server monitoring to the Hiring Assistant running on AWS
(see `codedeploy_steps.md`). Every step is done by hand in the AWS console or over SSH.

Versions used (latest on 3 Nov 2026): **Node Exporter 1.12.1**, **Prometheus 3.15.1**,
**Grafana OSS 13.3.0**.

---

## The big picture

```text
 App servers (made by hiring-agent-asg)          Monitoring server (one EC2)
 ┌──────────────────────────────┐               ┌─────────────────────────────────┐
 │ api + frontend containers    │               │ Prometheus :9090                │
 │ Node Exporter :9100 ◄────────┼── every 15s ──┤  - asks AWS "which app servers  │
 │  (CPU, RAM, disk, network)   │    scrape     │    are running?" (EC2 discovery)│
 └──────────────────────────────┘               │  - stores their metrics         │
                                                │ Grafana :3000 ──reads──► Prom.  │
                                                └─────────────────────────────────┘
                                                              ▲
                                                     you, in the browser
```

| Piece | What it does |
| --- | --- |
| **Node Exporter** | A tiny program on each app server. It shows the server's CPU, RAM, disk and network numbers on port **9100** (`/metrics`). |
| **Prometheus** | Visits every Node Exporter every 15 seconds, saves the numbers, and lets you query them (PromQL). |
| **EC2 service discovery** | The auto scaling group adds and removes servers on its own, so we can't type their IPs into Prometheus. Instead Prometheus asks AWS for all running servers with our tag. |
| **Grafana** | Draws dashboards from the numbers in Prometheus. |

**Application monitoring vs infrastructure monitoring:** Langfuse (Stages 8-10) shows what
the *agent* did: plans, tool calls, guard blocks. Prometheus + Grafana show how the *servers*
are doing: is the CPU full, is the RAM running out, is the disk almost full.

---

## Step 0. Before you start (one-time AWS setup)

These are needed by steps 3-5. They are not in the list of 9 steps, but nothing works
without them.

### 0a. Security group for the monitoring server: `hiring-agent-monitoring-sg`

**VPC → Security groups → Create security group** (default VPC)

| Direction | Port | From |
| --- | --- | --- |
| Inbound | 22 (SSH) | your IP only |
| Inbound | 9090 (Prometheus UI) | your IP only |
| Inbound | 3000 (Grafana UI) | your IP only |
| Outbound | all | `0.0.0.0/0` |

### 0b. Let the monitoring server reach Node Exporter

Edit **`hiring-agent-instance-sg`** (the app servers' security group) and add one rule:

| Direction | Port | From |
| --- | --- | --- |
| Inbound | 9100 | `hiring-agent-monitoring-sg` (only the monitoring server) |

Port 9100 is never opened to the internet.

### 0c. IAM role for the monitoring server: `hiring-agent-monitoring-role`

Prometheus calls AWS to list the running servers, so it needs permission to do that (read-only).

**IAM → Roles → Create role** → trusted entity **AWS service → EC2** → skip the managed
policies → name **`hiring-agent-monitoring-role`** → create. Then open the role →
**Add permissions → Create inline policy → JSON**, paste this, name it
**`ec2-describe-instances`**:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "ec2:DescribeInstances",
      "Resource": "*"
    }
  ]
}
```

### 0d. Launch the monitoring server

**EC2 → Instances → Launch instances**

- Name: **`hiring-agent-monitoring`**
- Image (AMI): **Ubuntu Server 24.04 LTS**, **64-bit (x86)**
- Instance type: **t3.small** (Prometheus + Grafana need about 1 GB of RAM)
- Key pair: your key
- Network: default VPC, a public subnet, **Auto-assign public IP: Enable**
- Security group: **`hiring-agent-monitoring-sg`**
- Storage: **20 GiB gp3**
- Advanced details → **IAM instance profile:** **`hiring-agent-monitoring-role`**

This server is **not** part of the auto scaling group. It is one fixed server.

---

## Step 1. Add Node Exporter to the launch template (version 2)

The auto scaling group builds every new app server from the launch template
`hiring-agent-lt`. If Node Exporter is in its user data, every new server gets it
automatically, including the ones the group adds when the CPU is busy.

**EC2 → Launch templates → `hiring-agent-lt` → Actions → Modify template (Create new version)**

- Template version description: **`v2 - node exporter`**
- Keep every other setting the same.
- **Resource tags → Add new tag** (Resource types: **Instances**). Prometheus finds the
  servers by these tags:

  | Key | Value |
  | --- | --- |
  | `application_name` | `hiring_agent` |
  | `Name` | `hiring-agent-app` |

- **Advanced details → User data:** replace the old script with the one below. The first
  part is the old CodeDeploy agent install (unchanged); the second part is new.

```bash
#!/bin/bash
set -euxo pipefail

# ---- Part 1: CodeDeploy agent (same as version 1) ----
apt update
apt install -y wget
cd /home/ubuntu
wget https://aws-codedeploy-ap-south-1.s3.ap-south-1.amazonaws.com/latestv2/install
chmod +x ./install
./install auto
systemctl enable --now codedeploy-agent

# ---- Part 2: Node Exporter (new in version 2) ----
NODE_EXPORTER_VERSION="1.12.1"

# its own system user, with no login and no home folder
id -u node_exporter &>/dev/null || useradd --no-create-home --shell /usr/sbin/nologin node_exporter

cd /tmp
wget -q https://github.com/prometheus/node_exporter/releases/download/v${NODE_EXPORTER_VERSION}/node_exporter-${NODE_EXPORTER_VERSION}.linux-amd64.tar.gz
tar xzf node_exporter-${NODE_EXPORTER_VERSION}.linux-amd64.tar.gz
mv node_exporter-${NODE_EXPORTER_VERSION}.linux-amd64/node_exporter /usr/local/bin/node_exporter
chown node_exporter:node_exporter /usr/local/bin/node_exporter
rm -rf node_exporter-${NODE_EXPORTER_VERSION}.linux-amd64*

# run it as a service, so it starts again after a reboot or a crash
cat >/etc/systemd/system/node_exporter.service <<'EOF'
[Unit]
Description=Prometheus Node Exporter
After=network.target

[Service]
User=node_exporter
Group=node_exporter
Type=simple
ExecStart=/usr/local/bin/node_exporter
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now node_exporter
```

Click **Create template version**. The list of versions now shows version **2**.

> Why a service and not `./node_exporter &`? A service starts again by itself if the server
> reboots or the program crashes. User data runs only once, on the first boot.

---

## Step 2. Replace the old app servers with version 2 servers

The running app server was built from version 1, so it has no Node Exporter. We replace it.

**First, check the ASG uses the new version:**
**EC2 → Auto Scaling groups → `hiring-agent-asg` → Details → Launch template.**
It should say version **Latest** (set in `codedeploy_steps.md` step 10), which is now
version 2. If it shows a fixed version, click **Edit** and pick **Latest**.

**Then start an instance refresh** (the ASG swaps the servers one by one for you):
**`hiring-agent-asg` → Instance refresh → Start instance refresh**

- Minimum healthy percentage: **100**
- Maximum healthy percentage: **200** (a new server starts **before** the old one stops, so
  the app stays up)
- Leave the rest as it is → **Start**

What happens: the ASG starts a version 2 server, CodeDeploy deploys the app on it (the launch
hook), the load balancer marks it healthy, then the ASG shuts down the old server. This takes
about 10-15 minutes. Watch it in **Instance refresh** and **Activity**.

**Check Node Exporter on the new server** (SSH into it):

```bash
systemctl status node_exporter --no-pager
```

```bash
curl -s localhost:9100/metrics | grep node_cpu_seconds_total | head -5
```

You should see `active (running)` and lines like `node_cpu_seconds_total{cpu="0",mode="idle"} ...`.
Also check the new server has the tags `application_name = hiring_agent` and
`Name = hiring-agent-app` (**EC2 → Instances → the server → Tags**).

---

## Step 3. Install Prometheus on the monitoring server

SSH into **`hiring-agent-monitoring`**, then:

```bash
cd ~
```

```bash
wget https://github.com/prometheus/prometheus/releases/download/v3.15.1/prometheus-3.15.1.linux-amd64.tar.gz
```

```bash
tar xvf prometheus-3.15.1.linux-amd64.tar.gz
```

```bash
cd prometheus-3.15.1.linux-amd64/
```

```bash
ls
```

You should see `prometheus` (the server), `promtool` (a checker) and `prometheus.yml` (the
config file).

---

## Step 4. Add EC2 service discovery to `prometheus.yml`

```bash
nano prometheus.yml
```

Go to the bottom of the file. Under `scrape_configs:` there is already one job,
`prometheus` (Prometheus watching itself). Add our job **under it, with the same
indentation** (2 spaces before `- job_name`). The end of the file should look like this:

```yaml
scrape_configs:
  - job_name: "prometheus"
    static_configs:
      - targets: ["localhost:9090"]
        labels:
          app: "prometheus"

  - job_name: 'hiring_agent_node_exporter'
    ec2_sd_configs:
      - region: ap-south-1
        port: 9100
        refresh_interval: 60s          # ask AWS for the server list every minute
        filters:
          - name: tag:application_name # only our app servers
            values: ['hiring_agent']
          - name: instance-state-name  # only running ones
            values: ['running']
    relabel_configs:
      # show the server's Name tag as a label
      - source_labels: [__meta_ec2_tag_Name]
        target_label: instance_name
      # show the instance id (i-0abc...) instead of an IP
      - source_labels: [__meta_ec2_instance_id]
        target_label: instance
      # scrape the private IP (both servers are in the same VPC)
      - source_labels: [__meta_ec2_private_ip]
        target_label: __address__
        replacement: '${1}:9100'
```

(The default job may look slightly different in your file; leave it as it is and only add
the `hiring_agent_node_exporter` block.)

What each part means:

| Part | Meaning |
| --- | --- |
| `ec2_sd_configs` | "Don't use a fixed list of IPs. Ask AWS which servers exist." This uses the IAM role from step 0c. |
| `filters` | Only servers with tag `application_name = hiring_agent` that are running. The monitoring server itself has no such tag, so it is left out. |
| `relabel_configs` | Rename AWS's details into readable labels, and point Prometheus at `<private IP>:9100`. |

Save and exit nano: **Ctrl + O**, **Enter**, **Ctrl + X**.

Check the file for mistakes (YAML is picky about spaces):

```bash
./promtool check config prometheus.yml
```

It should end with `SUCCESS`.

---

## Step 5. Start Prometheus in the background

```bash
nohup ./prometheus --config.file=prometheus.yml > prometheus.log 2>&1 &
```

- `&` runs it in the background, so you get your terminal back.
- `nohup` keeps it running after you close the SSH session (plain `&` would stop when you log out).
- Its output goes to `prometheus.log`. Look at it with `tail -f prometheus.log` (Ctrl + C to stop looking).

**Check it in the browser:** `http://<monitoring server public IP>:9090`

- **Status → Target health:** the `hiring_agent_node_exporter` job shows one target per app
  server, state **UP**. (It can take up to a minute: `refresh_interval: 60s`.)
- **Query** tab: type `node_memory_MemAvailable_bytes` → **Execute**. You see a number for
  each app server.

**If you change `prometheus.yml` later**, restart Prometheus:

```bash
pkill prometheus
```

```bash
nohup ./prometheus --config.file=prometheus.yml > prometheus.log 2>&1 &
```

---

## Step 6. Install Grafana and start it in the background

Still on the monitoring server:

```bash
cd ~
```

```bash
wget https://dl.grafana.com/oss/release/grafana-13.3.0.linux-amd64.tar.gz
```

```bash
tar -zxvf grafana-13.3.0.linux-amd64.tar.gz
```

```bash
cd grafana-13.3.0/
```

```bash
nohup ./bin/grafana server > grafana.log 2>&1 &
```

Same idea as Prometheus: `nohup` + `&`, output in `grafana.log`.

**Open it:** `http://<monitoring server public IP>:3000`
Log in with **admin / admin**. Grafana asks you to set a new password right away; do it.

---

## Step 7. Connect Grafana to Prometheus

**Connections → Data sources → Add data source → Prometheus**

- Prometheus server URL: **`http://localhost:9090`** (Prometheus and Grafana are on the same
  server, so `localhost` works)
- Leave the rest as it is → **Save & test**

You should see "Successfully queried the Prometheus API."

---

## Step 8. Import the "Node Exporter Full" dashboard

Grafana has a library of ready-made dashboards. **Node Exporter Full** (ID **1860**) has
panels for everything Node Exporter collects.

**Dashboards → New → Import**

- "Find and import dashboards...": type **`1860`** → **Load**
- Prometheus data source: pick the one from step 7
- Click **Import**. The dashboard opens right away.

At the top of the dashboard, pick:

- **Job:** `hiring_agent_node_exporter`
- **Host:** the app server's instance id (`i-0...`)

The panels now show the app server's CPU, memory, network and disk. The dashboard is saved,
so you can open it again later from **Dashboards → Node Exporter Full**.
