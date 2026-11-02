# Deploying the Hiring Assistant on AWS with CodeDeploy

This guide sets up AWS so the app deploys itself. You do the setup **once**, by hand, in the
AWS console. After that, every run of the **CD Pipeline** on GitHub puts the newest version of
the app on the servers automatically.

## The big picture

```
              Internet
                 │
        ┌────────▼──────────┐   port 80   → the Streamlit app (port 8501)
        │  hiring-agent-alb │   port 8000 → the FastAPI backend (port 8000)
        │  (load balancer)  │
        └────────┬──────────┘
                 │
        ┌────────▼─────────────────────┐
        │ hiring-agent-asg             │   1 to 3 EC2 servers (Ubuntu 24.04, t3.medium).
        │ (auto scaling group)         │   Each server runs both containers.
        └──────────────────────────────┘
                 ▲
   GitHub CD Pipeline → zip file in S3 → CodeDeploy puts it on every server
```

What each AWS piece does:

| Piece | What it is for |
| --- | --- |
| **ECR** | Stores the two Docker images (api and frontend). The CD Pipeline pushes them here. |
| **Secrets Manager** | Stores the API keys. Each server reads them when it deploys. |
| **S3 bucket** | Stores the deployment zip file (compose file + scripts). |
| **CodeDeploy** | Copies the zip file to each server and runs the scripts in it. |
| **Auto Scaling Group (ASG)** | Keeps 1 server running, and adds more (up to 3) when the CPU is busy. |
| **Load balancer (ALB)** | One public address for users. It sends each request to a healthy server. |

### What happens on a server during a deployment

CodeDeploy runs these steps in order. The scripts are in
[`deploy/codedeploy/scripts/`](deploy/codedeploy/scripts/).

| Step | Script | What it does |
| --- | --- | --- |
| 1. Stop | `application_stop.sh` | Stops the old containers and deletes the old images (nothing to do on the first deploy) |
| 2. Prepare | `before_install.sh` | Installs Docker and the AWS CLI (only the first time) and creates `/opt/hiring-agent` |
| 3. Copy | `appspec.yml` | Copies `docker-compose.yml` into `/opt/hiring-agent` |
| 4. Start | `application_start.sh` | Writes `.env` from Secrets Manager, logs in to ECR, pulls the images, starts the containers |
| 5. Check | `validate_service.sh` | Waits 30 seconds, then asks `/health` up to 3 times. If it is never `"healthy"`, CodeDeploy rolls back to the previous version |

### Before you start

- **Region:** `ap-south-1` (Mumbai). Create everything in this region.
- **Account ID:** `891377050051`. It is written in
  [`deploy/codedeploy/docker-compose.yml`](deploy/codedeploy/docker-compose.yml) and
  [`deploy/codedeploy/scripts/application_start.sh`](deploy/codedeploy/scripts/application_start.sh).
  If you use a different account, change it in both files.
- **Use the names exactly as written.** The scripts and
  [`.github/workflows/cd.yaml`](.github/workflows/cd.yaml) look for these names. If you change
  a name, change it in those files too.
- You do **not** need to create the ECR repositories. The CD Pipeline creates
  `hiring-agent-api` and `hiring-agent-frontend` on its first run.

---

## Step 1. Store the API keys in Secrets Manager

The servers need the API keys, but the keys must never go inside the Docker images. So we keep
them in Secrets Manager, and each server copies them into a `.env` file when it deploys.

**Secrets Manager → Store a new secret → Other type of secret → Key/value**

| Key | Value |
| --- | --- |
| `OPENAI_API_KEY` | your OpenAI key |
| `TYPESAFE_API_KEY` | your TypeSafe (Jev) key |
| `LANGFUSE_PUBLIC_KEY` | your Langfuse public key |
| `LANGFUSE_SECRET_KEY` | your Langfuse secret key |
| `LANGFUSE_BASE_URL` | for example `https://cloud.langfuse.com` |

- Secret name: **`hiring-agent-api-keys`**. Leave rotation off.
- Do **not** add `LANGFUSE_TRACING_ENABLED` or `LANGFUSE_TRACING_ENVIRONMENT`. The compose
  file already sets them (`true` and `production`).
- Do **not** add `DAGSHUB_USER_TOKEN`. Only the CI pipeline needs it.

---

## Step 2. Create an IAM user for GitHub Actions

GitHub needs its own AWS login to push images and start deployments.

**IAM → Users → Create user**

- Name: **`hiring-agent-github-actions`**. No console access.
- **Attach policies directly:** `AmazonEC2ContainerRegistryPowerUser` (lets it push images).
- Add an **inline policy** named **`hiring-agent-ecr-create`** (lets it create the two ECR
  repositories on the first run):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "CreateEcrRepos",
      "Effect": "Allow",
      "Action": ["ecr:CreateRepository", "ecr:DescribeRepositories"],
      "Resource": "*"
    }
  ]
}
```

- Add a second **inline policy** named **`hiring-agent-cd-deploy`** (lets it upload the zip
  file and start a deployment):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "PutRevision",
      "Effect": "Allow",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::hiring-agent-codedeploy/*"
    },
    {
      "Sid": "CreateDeployments",
      "Effect": "Allow",
      "Action": [
        "codedeploy:CreateDeployment",
        "codedeploy:GetDeployment",
        "codedeploy:GetDeploymentConfig",
        "codedeploy:RegisterApplicationRevision",
        "codedeploy:GetApplicationRevision"
      ],
      "Resource": "*"
    }
  ]
}
```

- Open the user → **Security credentials → Create access key → "Application running outside
  AWS"**. Copy the access key ID and the secret access key. The secret is shown only once.

## Step 3. Add the AWS keys to GitHub

**GitHub repository → Settings → Secrets and variables → Actions → New repository secret**

- `AWS_ACCESS_KEY_ID` = the access key ID from step 2
- `AWS_SECRET_ACCESS_KEY` = the secret access key from step 2

The other secrets (`OPENAI_API_KEY`, `DAGSHUB_USER_TOKEN`, ...) are already there for CI.

---

## Step 4. Create the S3 bucket

The CD Pipeline uploads the deployment zip file here, and CodeDeploy downloads it from here.

**S3 → Create bucket**

- Name: **`hiring-agent-codedeploy`**. Bucket names must be unique across all of AWS. If the
  name is taken, pick another one and change `DEPLOY_BUCKET` in `cd.yaml` and the bucket name
  in the policies (steps 2 and 5).
- Block **all** public access: **on**
- Encryption: leave the default.

---

## Step 5. Create two IAM roles

A role gives an AWS service permission to do things. We need one for the servers and one for
CodeDeploy.

### 5a. Role for the servers: `hiring-agent-instance-role`

The servers must pull images from ECR, read the API keys and download the zip file.

**IAM → Roles → Create role → AWS service → EC2**

- Attach: **`AmazonEC2ContainerRegistryReadOnly`** (pull images).
- Name: **`hiring-agent-instance-role`**.
- After it is created, add an **inline policy** named **`hiring-agent-instance-inline`**:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadApiKeysSecret",
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "arn:aws:secretsmanager:ap-south-1:891377050051:secret:hiring-agent-api-keys-*"
    },
    {
      "Sid": "ReadCodeDeployBundles",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:ListBucket"],
      "Resource": [
        "arn:aws:s3:::hiring-agent-codedeploy",
        "arn:aws:s3:::hiring-agent-codedeploy/*"
      ]
    }
  ]
}
```

### 5b. Role for CodeDeploy: `hiring-agent-codedeploy-role`

**IAM → Roles → Create role → AWS service → CodeDeploy → use case "CodeDeploy"**

- The policy **`AWSCodeDeployRole`** is added for you.
- Name: **`hiring-agent-codedeploy-role`**.

> If a deployment later fails with "access denied" on `iam:PassRole`, `ec2:RunInstances` or
> `ec2:CreateTags`, add those three actions to this role. CodeDeploy sometimes needs them
> when the auto scaling group uses a launch template.

---

## Step 6. Create two security groups

A security group is a firewall: it says which ports are open and to whom.

**VPC → Security groups → Create security group** (use the default VPC).

### `hiring-agent-alb-sg` (for the load balancer)

| Direction | Port | From |
| --- | --- | --- |
| Inbound | 80 | `0.0.0.0/0` (anyone) |
| Inbound | 8000 | `0.0.0.0/0` (anyone) |
| Outbound | all | `0.0.0.0/0` |

### `hiring-agent-instance-sg` (for the servers)

| Direction | Port | From |
| --- | --- | --- |
| Inbound | 8501 | `hiring-agent-alb-sg` (only the load balancer) |
| Inbound | 8000 | `hiring-agent-alb-sg` (only the load balancer) |
| Inbound | 22 (SSH) | your IP only (optional, for debugging) |
| Outbound | all | `0.0.0.0/0` |

> ⚠️ **Port 8000 is open to everyone.** Anyone who finds the address can call
> `POST /agent/run`, and every run spends OpenAI and Jev credits. For real use, allow
> port 8000 only from your own IP, or remove that listener in step 8. The Streamlit app does
> not need it: it talks to the API inside the server.

---

## Step 7. Create two target groups

A target group is the list of servers the load balancer can send traffic to, plus a health
check to know which servers are working.

**EC2 → Target groups → Create target group** (type **Instances**, protocol **HTTP**, default VPC)

### `hiring-agent-frontend-tg`

- Port: **8501**
- Health check path: **`/_stcore/health`** (Streamlit's built-in health page)
- Advanced health check settings: healthy threshold 2, unhealthy threshold 3, timeout 5s,
  interval 30s, success code 200
- After it is created: **Attributes → Edit → Stickiness: on**, type **Load balancer
  generated cookie**, duration **1 day**.

  Why: each server keeps its own uploaded files (the workspace lives inside its api
  container). Stickiness keeps a user on the same server, so a page refresh does not show a
  different, empty workspace.

### `hiring-agent-api-tg`

- Port: **8000**
- Health check path: **`/docs`**
- Same advanced settings as above

  Why not `/health`? Our `/health` makes a real call to OpenAI and to Jev. The load balancer
  checks every 30 seconds on every server, so that would cost money all day. `/docs` is free.
  `/health` is still used once per deployment, by `validate_service.sh`.

Do **not** add servers to the target groups yourself. The auto scaling group does it (step 10).

---

## Step 8. Create the load balancer

**EC2 → Load balancers → Create load balancer → Application Load Balancer**

- Name: **`hiring-agent-alb`**
- Scheme: **Internet-facing**, IPv4
- Network: the default VPC, pick **at least 2 public subnets** (in different zones)
- Security group: **`hiring-agent-alb-sg`** (remove the default one)
- Listener: **HTTP : 80** → forward to **`hiring-agent-frontend-tg`**
- After it is created:
  - **Listeners → Add listener → HTTP : 8000** → forward to **`hiring-agent-api-tg`**
  - **Attributes → Edit → Connection idle timeout: 600 seconds**. Screening all resumes takes
    a few minutes; the default (60 seconds) would cut long requests.

Copy the load balancer's **DNS name**. That is the app's public address.

---

## Step 9. Create the launch template

A launch template is the recipe for a new server. The auto scaling group uses it every time
it starts a server.

**EC2 → Launch templates → Create launch template**

- Name: **`hiring-agent-lt`**
- Image (AMI): **Ubuntu Server 24.04 LTS**, **64-bit (x86)**
- Instance type: **t3.medium**
- Key pair: your key (so you can SSH in to debug)
- Network settings → Security groups: **`hiring-agent-instance-sg`**. Leave the subnet as
  "Don't include in launch template".
- Advanced details:
  - **IAM instance profile:** **`hiring-agent-instance-role`**
  - **Storage:** one **gp3** volume of **20 GiB**
  - **User data:** paste the script below. It installs the CodeDeploy agent, the small
    program that lets CodeDeploy run our scripts on the server.

```bash
#!/bin/bash
set -euxo pipefail
apt update
apt install -y wget
cd /home/ubuntu
wget https://aws-codedeploy-ap-south-1.s3.ap-south-1.amazonaws.com/latestv2/install
chmod +x ./install
./install auto
systemctl enable --now codedeploy-agent
systemctl status codedeploy-agent --no-pager
```

Docker and the AWS CLI are not installed here. `before_install.sh` installs them during the
first deployment.

---

## Step 10. Create the auto scaling group

**EC2 → Auto Scaling groups → Create Auto Scaling group**

- Name: **`hiring-agent-asg`**
- Launch template: **`hiring-agent-lt`**, version **Latest**
- Network: the default VPC, the **same subnets** as the load balancer
- Load balancing: **Attach to an existing load balancer → Choose from your target groups** →
  select **both** `hiring-agent-frontend-tg` and `hiring-agent-api-tg`
- Health checks: turn on **Elastic Load Balancing health checks**. Grace period: **600
  seconds** (the first deployment installs Docker, which takes a few minutes).
- Group size: **desired 1, minimum 1, maximum 3**
- Automatic scaling: **Target tracking policy**
  - Metric: **Average CPU utilization**
  - Target value: **50**
  - Instance warmup: **300** seconds

The group starts one server right away. It has the CodeDeploy agent, but not the app yet.

---

## Step 11. Set up CodeDeploy

### 11a. Create the application

**CodeDeploy → Applications → Create application**

- Name: **`hiring-agent`**
- Compute platform: **EC2/On-premises**

### 11b. Create the deployment group

Open the application → **Create deployment group**

- Name: **`hiring-agent-dg`**
- Service role: **`hiring-agent-codedeploy-role`**
- Deployment type: **In-place**
- Environment configuration: **Amazon EC2 Auto Scaling groups** → **`hiring-agent-asg`**
- Deployment settings: **`CodeDeployDefault.OneAtATime`** (update one server at a time)
- Load balancer: tick **Enable load balancing** and select **both** target groups
- Advanced → Rollbacks: tick **Roll back when a deployment fails**

Because the deployment group is linked to the auto scaling group, every new server the group
starts later gets the latest working version of the app automatically before it gets any
traffic.

---

## Step 12. Run the first deployment

1. Commit and push `deploy/codedeploy/`, `.github/workflows/cd.yaml` and this file to `main`.
   GitHub only runs workflow files that are on `main`.
2. **GitHub → Actions → CD Pipeline → Run workflow**.
3. The pipeline has two jobs:
   - **Build & push images to ECR:** creates the ECR repositories (first run only), builds
     both images and pushes them.
   - **Roll out to EC2 via CodeDeploy:** zips `deploy/codedeploy/`, uploads it to S3 and
     starts a deployment. Its log prints a link to the deployment in the AWS console.
4. **CodeDeploy → Deployments →** open the running deployment → open the server's **events**
   to watch each step. The first deployment takes about 4–6 minutes.

---

## After the first deployment

You don't need to do anything by hand again.

- When the CI pipeline promotes a new champion, the CD Pipeline runs by itself: build, push,
  deploy.
- To change an API key, edit `hiring-agent-api-keys` in Secrets Manager, then re-run the CD
  Pipeline (**Actions → CD Pipeline → Run workflow**). Every deployment writes a fresh `.env`.
