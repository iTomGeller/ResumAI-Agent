import os

import paramiko

host = os.environ["RESUMAI_SSH_HOST"]
username = os.environ.get("RESUMAI_SSH_USER", "root")
ssh_password = os.environ.get("RESUMAI_SSH_PASSWORD")
mysql_password = os.environ["RESUMAI_MYSQL_PASSWORD"]

ssh = paramiko.SSHClient()
ssh.load_system_host_keys()
ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
ssh.connect(
    host,
    username=username,
    password=ssh_password,
    look_for_keys=ssh_password is None,
    allow_agent=ssh_password is None,
    timeout=15,
)

# Check docker-compose for mysql password
stdin, stdout, stderr = ssh.exec_command('grep -A10 "mysql" /opt/ai-resume-agent-platform/docker-compose.yml | grep -i "password\\|MYSQL"', timeout=15)
print("MySQL env:", stdout.read().decode('utf-8', errors='replace'))

# The password is supplied through stdin, so it is never interpolated into the
# remote command line or committed to this repository.
fix_sql = (
    "UPDATE resume_task SET status='FAILED', queue_status='FAILED', "
    "fail_reason='manual recovery', update_time=NOW() WHERE id=1038;"
)
remote = (
    "docker exec -i resumai-mysql sh -c "
    "'read -r MYSQL_PWD; export MYSQL_PWD; exec mysql -uroot resume_ai'"
)
stdin, stdout, stderr = ssh.exec_command(remote, timeout=15)
stdin.write(mysql_password + "\n" + fix_sql + "\n")
stdin.flush()
stdin.channel.shutdown_write()
print("Fix result:", stdout.read().decode("utf-8", errors="replace"), stderr.read().decode("utf-8", errors="replace"))

ssh.close()
