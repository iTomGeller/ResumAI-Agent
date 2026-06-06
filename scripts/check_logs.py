import paramiko
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('8.166.136.122', username='root', password='AS123456hyj', look_for_keys=False, allow_agent=False, timeout=20)

# Find backend container name
stdin, stdout, stderr = ssh.exec_command('docker ps --format "{{.Names}}" | grep -i backend', timeout=15)
print("Backend containers:", stdout.read().decode('utf-8', errors='replace'))

# Restart with docker restart
stdin, stdout, stderr = ssh.exec_command('docker restart ai-resume-backend', timeout=60)
print("Restart:", stdout.read().decode('utf-8', errors='replace'))
print("Err:", stderr.read().decode('utf-8', errors='replace'))

ssh.close()
