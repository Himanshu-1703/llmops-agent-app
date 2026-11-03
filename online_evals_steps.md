# Online Evals for the Hiring Assistant

Offline evals (Stage 2) test the agent on 9 goldens **before** a release. Online evals check
the agent on **real tasks** after it is deployed. There is no golden for a real task, so we
can only use metrics that do not need a correct answer.

This guide sets up a small, separate EC2 server that runs one Python script every 15 minutes.
The script finds the new traces in Langfuse, grades each one with our two judges, and writes the
scores back onto the traces. You do the setup **once**, by hand.

## The big picture

```
  Users ──► the app (CodeDeploy servers) ──► Langfuse traces (environment "production")
                                                     ▲            │
                                     write 2 scores  │            │ read new traces
                                                     │            ▼
                                  online-evals server: run_online_eval.py (cron, every 15 min)
```

The online-evals server never talks to the app. It only reads traces from Langfuse and writes
scores to Langfuse.

### The two scores

| Score in Langfuse | Judge | Question it answers |
| --- | --- | --- |
| `plan_quality_sees_workspace` | Plan Quality (sees workspace) | Was the plan complete and correct for the files the planner saw? |
| `plan_adherence_custom` | Plan Adherence (custom) | Did the executor do each plan step as written? |

Both are the same judges as in the offline eval (`gpt-5.4-mini`, pass mark 0.7).

### What the script does on every run

The script is [`src/hiring_agent/evals/online_evals/run_online_eval.py`](src/hiring_agent/evals/online_evals/run_online_eval.py).

| Step | What it does |
| --- | --- |
| 1. Find | Asks Langfuse for the `hiring-agent-run` traces (environment `production`) started since the last run |
| 2. Read | Reads what the judges need from each trace (table below) |
| 3. Skip | Skips a trace with no plan: blocked by a guardrail, or the run crashed |
| 4. Grade | Runs both judges on the trace |
| 5. Write | Writes both scores onto the trace, with the judge's reason as the comment |
| 6. Remember | Saves the time of this run in `eval_checkpoint.json`, so the next run only looks at newer traces |

Where each input comes from in a trace:

| The judge needs | Where it is in the trace |
| --- | --- |
| the task | the root span `hiring-agent-run`, its input |
| the plan and what the executor did per step | the `responder` step, its input (`plan` and `past_steps`) |
| the files the planner saw (workspace snapshot) | the planner's LLM call, the user message, after `WORKSPACE SNAPSHOT:` |
| the tool list | the planner's LLM call, the system message, after `TOOLS:` |

Why read the trace and not the workspace folder? By the time the script runs, the workspace has
changed (resumes moved, rows added), and it is on another server anyway. The trace keeps exactly
what the planner saw.

---

## Step 1. Store the API keys in Secrets Manager

The server needs an OpenAI key (for the judges) and the Langfuse keys (to read traces and
write scores). It does **not** need the TypeSafe key, because the script runs no guardrails.

**Secrets Manager → Store a new secret → Other type of secret → Key/value**

| Key | Value |
| --- | --- |
| `OPENAI_API_KEY` | your OpenAI key |
| `LANGFUSE_PUBLIC_KEY` | your Langfuse public key |
| `LANGFUSE_SECRET_KEY` | your Langfuse secret key |
| `LANGFUSE_BASE_URL` | for example `https://cloud.langfuse.com` |

- Secret name: **`hiring-agent-online-evals`**. Leave rotation off.

---

## Step 2. Upload the script to S3

The server downloads the script from an S3 bucket, so you never copy files to it by hand.

**S3 → Create bucket**

- Name: **`hiring-agent-online-evals`**. Bucket names must be unique across all of AWS. If the
  name is taken, pick another one and use it everywhere below.
- Block **all** public access: **on**

Then upload the two files from the project root:

```bash
aws s3 cp src/hiring_agent/evals/online_evals/run_online_eval.py s3://hiring-agent-online-evals/ --region ap-south-1
```

```bash
aws s3 cp src/hiring_agent/evals/online_evals/requirements-online-evals.txt s3://hiring-agent-online-evals/ --region ap-south-1
```

`requirements-online-evals.txt` pins the exact versions the script was tested with
(`deepeval==4.2.8`, `langfuse==5.1.0`, `python-dotenv==1.2.4`). If you upgrade one of them
locally, write the file again before uploading:

```bash
uv pip freeze | grep -E '^(deepeval|langfuse|python-dotenv)==' > src/hiring_agent/evals/online_evals/requirements-online-evals.txt
```

---

## Step 3. Create an IAM role for the server

The server must read the secret and download the two files from S3.

**IAM → Roles → Create role → AWS service → EC2**

- Do not attach any AWS policy.
- Name: **`hiring-agent-online-evals-role`**.
- After it is created, add an **inline policy** named **`hiring-agent-online-evals-inline`**:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadKeysSecret",
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "arn:aws:secretsmanager:ap-south-1:891377050051:secret:hiring-agent-online-evals-*"
    },
    {
      "Sid": "ReadScript",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:ListBucket"],
      "Resource": [
        "arn:aws:s3:::hiring-agent-online-evals",
        "arn:aws:s3:::hiring-agent-online-evals/*"
      ]
    }
  ]
}
```

---

## Step 4. Launch the server

One small server is enough: the script only waits for OpenAI and Langfuse.

**EC2 → Instances → Launch instances**

- Name: **`hiring-agent-online-evals`**
- Image (AMI): **Ubuntu Server 24.04 LTS**, **64-bit (x86)**
- Instance type: **t3.small**
- Key pair: your key (you will SSH in for step 5)
- Network: the default VPC. Security group: allow **SSH (22) from your IP only**. Nothing
  else: the server receives no traffic, it only sends requests out.
- Storage: **15 GiB** gp3
- Advanced details → **IAM instance profile:** **`hiring-agent-online-evals-role`**

---

## Step 5. Set up the server

SSH in as `ubuntu`. Paste **one block at a time** and wait for it to finish. Don't add `sudo`
where it isn't shown: a folder made by `root` can't be written to later.

### 5a. Install the tools

`NEEDRESTART_MODE=a` stops Ubuntu's "restart services?" screen from eating what you paste next.

```bash
sudo apt-get update
sudo NEEDRESTART_MODE=a apt-get install -y jq unzip cron
```

Install the AWS CLI. The last line deletes the installer files, so they don't fill `/tmp`:

```bash
curl -s https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -o /tmp/awscliv2.zip
unzip -q /tmp/awscliv2.zip -d /tmp
sudo /tmp/aws/install
rm -rf /tmp/aws /tmp/awscliv2.zip
```

Check that the server has its role. This should print an ARN with `hiring-agent-online-evals-role`:

```bash
aws sts get-caller-identity
```

### 5b. Create the folder and get the script

Everything lives in `/opt/hiring-agent`, owned by you:

```bash
sudo mkdir -p /opt/hiring-agent/online-evals
sudo chown -R "$(id -un):$(id -gn)" /opt/hiring-agent
```

```bash
aws s3 cp s3://hiring-agent-online-evals/run_online_eval.py /opt/hiring-agent/online-evals/ --region ap-south-1
aws s3 cp s3://hiring-agent-online-evals/requirements-online-evals.txt /opt/hiring-agent/online-evals/ --region ap-south-1
```

### 5c. Install Python and the libraries

We use [uv](https://docs.astral.sh/uv/) to install Python 3.12 and the three libraries. All of
it stays inside `/opt/hiring-agent`.

```bash
curl -LsSf https://astral.sh/uv/install.sh | env UV_UNMANAGED_INSTALL=/opt/hiring-agent/bin sh
```

```bash
UV_PYTHON_INSTALL_DIR=/opt/hiring-agent/python /opt/hiring-agent/bin/uv venv --clear --python 3.12 /opt/hiring-agent/venv
```

```bash
UV_CACHE_DIR=/opt/hiring-agent/.uv-cache /opt/hiring-agent/bin/uv pip install --python /opt/hiring-agent/venv/bin/python -r /opt/hiring-agent/online-evals/requirements-online-evals.txt
```

Check the versions. You should see `deepeval==4.2.8`, `langfuse==5.1.0` and `python-dotenv==1.2.4`:

```bash
/opt/hiring-agent/bin/uv pip freeze --python /opt/hiring-agent/venv/bin/python | grep -Ei '^(deepeval|langfuse|python-dotenv)=='
```

### 5d. Write the keys into `.env`

```bash
aws secretsmanager get-secret-value --secret-id hiring-agent-online-evals --region ap-south-1 \
  --query SecretString --output text \
  | jq -r 'to_entries[] | "\(.key)=\(.value)"' > /opt/hiring-agent/.env
chmod 600 /opt/hiring-agent/.env
```

The script finds this file by itself: it looks in its own folder, then in the folders above.

### 5e. Run it every 15 minutes

Create a small script for cron to call:

```bash
cat > /opt/hiring-agent/run_eval.sh <<'EOF'
#!/bin/bash
cd /opt/hiring-agent/online-evals
exec /usr/bin/flock -n /tmp/online_eval.lock /opt/hiring-agent/venv/bin/python run_online_eval.py >> cron.log 2>&1
EOF
chmod +x /opt/hiring-agent/run_eval.sh
```

```bash
echo '*/15 * * * * /opt/hiring-agent/run_eval.sh' | crontab -
```

What each part does:

| Part | Why |
| --- | --- |
| `cd .../online-evals` | The checkpoint file is written next to the script |
| `flock -n` | If the last run is still going (a big trace can take a while), skip this one instead of running two at once |
| full path to `venv/bin/python` | Cron does not know about the venv |
| `>> cron.log 2>&1` | Keeps every run's output in one log file |
| `*/15 * * * *` | At :00, :15, :30 and :45 of every hour |

When it is done, the folder looks like this:

```
/opt/hiring-agent/
├── .env                               keys, from Secrets Manager
├── bin/uv                             installs Python and the libraries
├── python/                            Python 3.12
├── venv/                              the libraries
├── run_eval.sh                        what cron calls
└── online-evals/
    ├── run_online_eval.py             from S3
    ├── requirements-online-evals.txt  from S3
    ├── eval_checkpoint.json           made by the first run; don't delete it
    └── cron.log                       the output of every run
```

---

## Step 6. Check that it works

Run a task in the deployed app (for example "Screen all resumes ..."). Then, on the server,
run the job once by hand instead of waiting for cron:

```bash
/opt/hiring-agent/run_eval.sh
```

Read the log. The last line should be `Run complete: fetched=1 scored=1 skipped=0 failed=0`:

```bash
tail -n 20 /opt/hiring-agent/online-evals/cron.log
```

Check that cron has the job:

```bash
crontab -l
```

Open the trace in Langfuse: its **Scores** tab shows `plan_quality_sees_workspace` and
`plan_adherence_custom`. In the trace list you can filter by a score (for example Plan
Adherence below 0.7) to find the runs that went wrong.

To watch new runs as they happen:

```bash
tail -F /opt/hiring-agent/online-evals/cron.log
```

---

## After the setup

- **Nothing more to do by hand.** Every 15 minutes the new traces get their scores.
- **If you change the script or a judge**, upload it to S3 again (step 2), then on the server
  download it again (the two `aws s3 cp` lines in step 5b). Cron uses the new file on its next run.
- **If you change a key**, edit `hiring-agent-online-evals` in Secrets Manager, then write
  `.env` again (step 5d).
