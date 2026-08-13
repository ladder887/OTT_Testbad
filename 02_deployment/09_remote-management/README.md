# `ott-control` 원격 설치 도구

이 문서는 고정 IP 설정이 끝난 뒤 `ott-control`에서 실행할 설치 순서를 설명한다.
연구 문서가 제외된 Git clone에서도 이 문서만으로 배포할 수 있다.

## 장비 구분

- `ott-control` (`.150`): 나머지 17대에 설치 명령을 내리는 Pi
- 서버·Edge·Client Pi 17대: 코드를 설치하고 서비스를 실행할 장비

Ansible inventory에는 17대만 들어 있다. 명령의 `all`은 이 17대를 뜻하며
`ott-control`은 포함하지 않는다.

## 파일 역할

| 파일 | 실행 시점 | 역할 |
|---|---|---|
| `playbooks/00_bootstrap_fresh_os.yml` | 최초 1회 | Docker와 `ottadmin` SSH key 계정 설치 |
| `playbooks/00_preflight.yml` | 배포 전 | hostname, IP, Docker, NTP, `eth0` 검사 |
| `playbooks/01_sync_repository.yml` | 코드 배포 | 동일한 Git commit을 17대에 checkout |
| `playbooks/02_deploy_platform.yml` | 서버 배포 | Storage, Origin, Edge, Processing 실행 |
| `playbooks/03_deploy_clients.yml` | 영상 준비 후 | Client Pi마다 logical client 10개 실행 |
| `playbooks/04_verify.yml` | 마지막 | media, DB, LIVE, container, health 전체 검증 |
| `playbooks/90_stop_clients.yml` | 실험 정지 | logical client만 정지 |

## 설치 순서 요약

`ott-control`에서 실행한다.

```bash
cd ~/OTT_Testbad/02_deployment/09_remote-management
```

### 1. Ansible과 SSH key 준비

```bash
bash scripts/install_control_node.sh

ssh-keygen -t ed25519 -a 100 \
  -f ~/.ssh/ott_lab_ed25519 \
  -C "ott-lab-control"
```

### 2. 새 OS 17대 최초 준비

```bash
./scripts/run.sh playbooks/00_bootstrap_fresh_os.yml \
  -e ansible_user=lee \
  --ask-pass \
  --ask-become-pass
```

이 단계만 초기 OS 계정 `lee`와 password를 사용한다. password는 파일에 저장하지
않는다. 이후 모든 명령은 inventory에 설정된 `ottadmin`과 SSH key를 사용한다.

### 3. 사전 검사

```bash
./scripts/run.sh playbooks/00_preflight.yml
```

### 4. 비밀값 준비

```bash
cp inventory/vault.example.yml inventory/vault.yml
nano inventory/vault.yml
~/.venvs/ott-ansible/bin/ansible-vault encrypt inventory/vault.yml
```

### 5. 동일 commit 배포

```bash
REVISION="$(git -C ../../.. rev-parse HEAD)"

./scripts/run.sh playbooks/01_sync_repository.yml \
  -e "deployment_revision=${REVISION}"
```

배포 revision은 remote repository에 push된 40자리 commit SHA여야 한다.

### 6. 서버와 Edge 실행

```bash
./scripts/run.sh playbooks/02_deploy_platform.yml \
  --ask-vault-pass
```

실행 순서:

```text
ott-storage -> ott-origin -> ott-edge-1~4 -> ott-processing
```

### 7. 영상 업로드 후 Client Pi 실행

```bash
./scripts/run.sh playbooks/03_deploy_clients.yml \
  --ask-vault-pass
```

### 8. 전체 검증

```bash
./scripts/run.sh playbooks/04_verify.yml
```

## 현재 주소

| hostname | IP |
|---|---:|
| `ott-origin` | `192.168.0.101` |
| `ott-edge-1~4` | `192.168.0.111~114` |
| `ott-storage` | `192.168.0.120` |
| `ott-processing` | `192.168.0.130` |
| `ott-user1~10` | `192.168.0.131~140` |
| `ott-control` | `192.168.0.150` |
| logical clients | `192.168.0.151~250` |

## 지켜야 할 조건

- `.151~.250`은 DHCP가 사용하면 안 된다.
- 모든 장비는 같은 commit을 사용한다.
- 원격 repository의 수동 수정은 자동으로 덮어쓰지 않는다.
- private key, `inventory/vault.yml`, `.env`는 Git에 저장하지 않는다.
- Origin media는 Git으로 전송하지 않는다.
- `playbooks/04_verify.yml`은 영상 업로드 전에는 media 검사에서 실패한다.
