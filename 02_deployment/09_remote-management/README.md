# `ott-control`에서 전체 장비 구축하기

이 문서는 고정 IP와 hostname 설정이 끝난 뒤 `ott-control`에서 실행할 명령을
설명한다. 연구 문서가 제외된 Git clone에서도 이 파일만으로 배포할 수 있다.

## 1. 먼저 알아둘 것

### 장비 구분

- `ott-control` (`192.168.0.150`): 아래 명령을 실행하는 Pi
- 서버·Edge·Client Pi 17대: 코드를 설치하고 service를 실행할 장비

Ansible의 `all`은 17대만 뜻하며 `ott-control`은 포함하지 않는다.

| 장비 | IP |
|---|---:|
| `ott-origin` | `192.168.0.101` |
| `ott-edge-1~4` | `192.168.0.111~114` |
| `ott-storage` | `192.168.0.120` |
| `ott-processing` | `192.168.0.130` |
| `ott-user1~10` | `192.168.0.131~140` |
| logical clients | `192.168.0.151~250` |

### 입력값 구분

| 값 | 처리 |
|---|---|
| 초기 `lee` OS password | 최초 SSH와 bootstrap의 `SSH password`, `BECOME password` |
| SSH key passphrase | 사용하지 않음, key 생성 시 빈 값 |
| 실험용 서비스 설정 | `inventory/lab.yml`에 포함되어 있으므로 별도 입력 없음 |

password를 입력할 때 terminal에 글자나 `*`가 표시되지 않는 것은 정상이다.

## 2. `ott-control` 기본 프로그램 설치

```bash
sudo apt update
sudo apt install -y \
  git python3 python3-venv openssh-client sshpass
```

- `apt update`: 설치 가능한 package 목록을 갱신한다.
- `apt install`: Git, Python, SSH, 최초 Ansible password 접속 도구를 설치한다.
- `sudo` 질문에는 `ott-control`의 초기 `lee` OS password를 입력한다.

확인:

```bash
git --version
python3 --version
ssh -V
```

세 명령 모두 version을 출력해야 한다.

`python3 --version`은 `3.12` 이상이어야 한다. 현재 고정한 `ansible-core 2.21.2`는
control 장비에서 Python 3.12 이상을 요구한다.

## 3. repository clone 또는 갱신

`~/OTT_Testbad`가 없으면 clone한다.

```bash
git clone https://github.com/ladder887/OTT_Testbad.git ~/OTT_Testbad
```

이미 있으면 다시 clone하지 않고 갱신한다.

```bash
cd ~/OTT_Testbad
git pull --ff-only
```

이 배포 방식은 17대가 repository를 직접 clone하므로 public read가 가능한 repository를
기준으로 한다. private repository라면 17대에도 별도 deploy credential이 필요하다.

현재 상태 확인:

```bash
cd ~/OTT_Testbad
git status --short
git rev-parse --show-toplevel
git rev-parse HEAD
```

- `git status --short`는 아무것도 출력하지 않아야 한다.
- root는 `/home/lee/OTT_Testbad`여야 한다.
- `HEAD`는 40자리 commit SHA를 출력한다.

## 4. Ansible 설치

```bash
cd ~/OTT_Testbad/02_deployment/09_remote-management
bash scripts/install_control_node.sh
```

`bash scripts/install_control_node.sh`의 정확한 동작:

1. `python3` 설치 여부와 Python 3.12 이상을 확인한다.
2. `/home/lee/.venvs/ott-ansible`에 별도 Python 환경을 만든다.
3. 그 환경의 `pip`를 갱신한다.
4. `requirements-control.txt`에 고정된 `ansible-core`를 설치한다.
5. 설치 version과 환경 위치를 출력한다.

이 script는 17대에 접속하지 않고, Docker나 OTT service도 설치하지 않는다.
`ott-control`의 사용자 home directory만 변경한다. `sudo`를 붙이지 않는다.

예상 진행 출력:

```text
[1/3] Creating the isolated Python environment: /home/lee/.venvs/ott-ansible
[2/3] Updating pip inside the isolated environment
[3/3] Installing the pinned ott-control dependencies
ott-control setup complete.
```

설치 확인:

```bash
~/.venvs/ott-ansible/bin/ansible --version
~/.venvs/ott-ansible/bin/ansible-playbook --version
```

`No module named venv`가 나오면 다음 명령 후 script를 다시 실행한다.

```bash
sudo apt install -y python3-venv
```

`ansible-core 2.21 requires Python 3.12`가 나오면 현재 64-bit Raspberry Pi OS로
`ott-control` OS를 맞춰야 한다.

## 5. 자동 배포용 SSH key 생성

```bash
ssh-keygen -t ed25519 -a 100 \
  -N "" \
  -f ~/.ssh/ott_lab_ed25519 \
  -C "ott-lab-control"
```

| 옵션 | 의미 |
|---|---|
| `-t ed25519` | key 방식 지정 |
| `-a 100` | private key KDF 반복 횟수 |
| `-N ""` | passphrase를 빈 값으로 지정하여 질문 생략 |
| `-f` | 파일 경로 지정 |
| `-C` | 식별용 설명 |

이미 `Enter passphrase ...` 질문이 나온 상태라면 아무것도 입력하지 않고 `Enter`,
확인 질문에서도 다시 `Enter`를 누른다. OS password를 입력하는 곳이 아니다.

확인:

```bash
ls -l ~/.ssh/ott_lab_ed25519 ~/.ssh/ott_lab_ed25519.pub
ssh-keygen -lf ~/.ssh/ott_lab_ed25519.pub
```

- `ott_lab_ed25519`: private key, `ott-control` 밖으로 복사하거나 Git에 저장하지 않는다.
- `ott_lab_ed25519.pub`: bootstrap이 17대에 설치할 public key다.
- 기존 key 덮어쓰기 질문이 나오면 `n`을 입력한다.

## 6. 17대 최초 SSH 확인

OS 재설치 전 host key 기록을 IP별로 제거한다.

```bash
for last in 101 111 112 113 114 120 130 \
            131 132 133 134 135 136 137 138 139 140; do
  ssh-keygen -R "192.168.0.${last}"
done
```

이 명령은 `ott-control`의 `known_hosts` 기록만 지우며 원격 Pi는 변경하지 않는다.

다음 블록은 17대에 초기 `lee` 계정으로 접속해 실제 hostname을 확인한다.

```bash
HOSTS=(
  "192.168.0.101 ott-origin"
  "192.168.0.111 ott-edge-1"
  "192.168.0.112 ott-edge-2"
  "192.168.0.113 ott-edge-3"
  "192.168.0.114 ott-edge-4"
  "192.168.0.120 ott-storage"
  "192.168.0.130 ott-processing"
  "192.168.0.131 ott-user1"
  "192.168.0.132 ott-user2"
  "192.168.0.133 ott-user3"
  "192.168.0.134 ott-user4"
  "192.168.0.135 ott-user5"
  "192.168.0.136 ott-user6"
  "192.168.0.137 ott-user7"
  "192.168.0.138 ott-user8"
  "192.168.0.139 ott-user9"
  "192.168.0.140 ott-user10"
)

for entry in "${HOSTS[@]}"; do
  read -r ip expected <<< "${entry}"
  echo "---- ${expected} (${ip}) ----"
  if ! actual="$(ssh \
      -o PubkeyAuthentication=no \
      -o PreferredAuthentications=password \
      "lee@${ip}" hostname)"; then
    echo "[FAIL] SSH connection failed: ${ip}"
    break
  fi
  if [[ "${actual}" != "${expected}" ]]; then
    echo "[FAIL] expected=${expected}, actual=${actual}"
    break
  fi
  echo "[OK] ${ip} -> ${actual}"
done
```

처음 보는 fingerprint 질문에는 해당 IP가 맞을 때 `yes`, password 질문에는 초기
`lee` OS password를 입력한다. 최대 17번 password를 묻는다.

17대 모두 `[OK]`여야 한다. `[FAIL]`이 한 줄이라도 나오면 전원, Ethernet, IP, SSH,
hostname을 수정한 후 다시 실행한다.

## 7. Docker와 `ottadmin`을 17대에 설치

```bash
cd ~/OTT_Testbad/02_deployment/09_remote-management

bash scripts/run.sh playbooks/00_bootstrap_fresh_os.yml \
  -e ansible_user=lee \
  --ask-pass \
  --ask-become-pass
```

명령 구성:

- `scripts/run.sh`: 전용 Ansible 환경과 이 directory의 inventory를 선택하는 wrapper
- `00_bootstrap_fresh_os.yml`: fresh OS 준비 작업 목록
- `-e ansible_user=lee`: 이번 한 번만 초기 계정 `lee`로 접속
- `--ask-pass`: SSH password 질문
- `--ask-become-pass`: 원격 `sudo` password 질문

입력:

```text
SSH password: [초기 lee password]
BECOME password[defaults to SSH password]: [같은 password]
```

playbook이 17대에서 하는 일:

1. Debian/Raspberry Pi OS 확인
2. Git, Python, curl, rsync 등 기본 package 설치
3. Docker 공식 repository 등록
4. Docker Engine과 Compose plugin 설치 및 자동 시작
5. key 전용 계정 `ottadmin` 생성
6. `ottadmin`을 Docker group에 추가
7. `ott_lab_ed25519.pub`를 `authorized_keys`에 설치
8. Origin의 `/srv/ott-media/{hls,source,thumbnails}` 생성

아직 repository clone, OTT service 실행, media 업로드, logical client 실행은 하지 않는다.

마지막 `PLAY RECAP`에서 17대 모두 `unreachable=0`, `failed=0`이어야 한다.

새 key 접속 확인:

```bash
bash scripts/ansible.sh all -m ping
```

`ansible.sh`는 playbook이 아닌 단일 Ansible module을 실행한다. 각 장비가
`SUCCESS`와 `"ping": "pong"`을 출력해야 한다.

## 8. 배포 전 사전 검사

```bash
bash scripts/run.sh playbooks/00_preflight.yml
```

변경 없이 다음을 검사한다.

- OS와 Python 3
- 실제 hostname과 inventory hostname
- 실제 IP와 inventory IP
- `ottadmin`의 Docker 권한과 Compose version
- NTP 시간 동기화
- Client Pi의 유선 `eth0`
- root filesystem 사용량

마지막에 `unreachable=0`, `failed=0`이어야 한다. `NTPSynchronized=no`는 log timestamp
비교를 망가뜨리므로 무시하지 않는다.

## 9. 실험용 공통 설정 확인

별도 password 생성, 파일 복사, 편집, 암호화는 하지 않는다. 폐쇄망 실험용 기본값이
`inventory/lab.yml`에 이미 들어 있으며 모든 서비스가 같은 값 `ottlab1234`를 사용한다.

확인만 한다.

```bash
cat inventory/lab.yml
```

Neo4j만 제품 동작 조건 때문에 8자 이상이어야 하며 현재 기본값은 이 조건을 만족한다.
나머지 길이 권장은 제거했다. 외부망 실험으로 바꿀 때만 이 파일의 값을 수정한다.

## 10. 같은 Git commit을 17대에 설치

Windows에서 변경 내용을 GitHub에 commit/push한 후 `ott-control`에서 실행한다.

```bash
cd ~/OTT_Testbad
git pull --ff-only
git status --short
REVISION="$(git rev-parse HEAD)"
echo "${REVISION}"
```

- `git status --short`가 비어 있어야 한다.
- `REVISION`은 40자리 배포 commit SHA다.

```bash
cd ~/OTT_Testbad/02_deployment/09_remote-management

bash scripts/run.sh playbooks/01_sync_repository.yml \
  -e "deployment_revision=${REVISION}"
```

playbook은 17대의 `/home/ottadmin/OTT_Testbad`에 repository를 clone/update하고, 모두
정확히 같은 SHA인지 확인한다. 원격 장비에 수동 수정이 있으면 덮어쓰지 않고 실패한다.

## 11. Platform server와 Edge 실행

```bash
bash scripts/run.sh playbooks/02_deploy_platform.yml
```

실행 순서:

```text
ott-storage
  -> ott-origin
  -> ott-edge-1 -> ott-edge-2 -> ott-edge-3 -> ott-edge-4
  -> ott-processing
```

playbook은 서비스 `.env` 작성, Compose 설정 검사, image build, container 실행, health
대기를 수행한다. Origin media directory의 기존 파일은 삭제하지 않는다.

endpoint 확인:

```bash
for url in \
  http://192.168.0.120:9200 \
  http://192.168.0.101:3001/health \
  http://192.168.0.101:8080/health \
  http://192.168.0.111/health \
  http://192.168.0.112/health \
  http://192.168.0.113/health \
  http://192.168.0.114/health; do
  echo "---- ${url} ----"
  curl -fsS "${url}" && echo
done
```

일곱 URL이 모두 정상 응답을 출력해야 한다.

## 12. VOD와 LIVE 업로드

브라우저에서 `http://192.168.0.101:5173/manage`를 연다.

- email: `admin@ott.com`
- password: `ottlab1234`

짧은 VOD 한 개를 먼저 올리고 1080p/720p 변환, Web 목록, Origin/Edge 재생을 확인한
후 나머지를 한 개씩 올린다. Raspberry Pi software encoding이므로 동시에 여러 개를
올리지 않는다.

목표 baseline:

- VOD 13개 이상
- LIVE 3개 이상
- 모든 콘텐츠에 1080p/720p playlist
- LIVE media sequence가 계속 증가

## 13. logical client 100개 실행

```bash
bash scripts/run.sh playbooks/03_deploy_clients.yml
```

playbook은 Client Pi 두 대씩 다음 작업을 수행한다.

1. 100개 client inventory와 Pi별 Compose 생성
2. ID, account, device, IP 중복/충돌 검사
3. logical client image build
4. Pi별 `.env` 작성
5. `ipvlan`으로 Pi마다 container 10개 실행
6. running container가 정확히 10개인지 확인

```text
ott-user1  .131 -> lc001~lc010 -> .151~.160
ott-user2  .132 -> lc011~lc020 -> .161~.170
...
ott-user10 .140 -> lc091~lc100 -> .241~.250
```

`ipvlan`은 하나의 물리 `eth0`를 공유하면서 container마다 별도 source IP를 사용한다.
공유기 DHCP에서 `.151~.250`을 제외해야 한다.

현재 logical client 기본 동작은 `idle`이다. 100개 container를 실행해 IP와 배포 구조를
준비하지만 정상/공격 시청 scenario를 자동 실행하지는 않는다.

`ott-user1`에서 확인:

```bash
ssh -i ~/.ssh/ott_lab_ed25519 ottadmin@192.168.0.131
cd ~/OTT_Testbad/03_experiments/07_generated/pi01
docker compose ps

docker compose exec -T lc001 \
  python /app/client_agent.py show-config

docker compose exec -T lc001 \
  python /app/client_agent.py probe
```

`lc001` probe 결과는 `status: 200`, configured source IP `.151`, Edge `.111`이어야 한다.
Edge log의 `client_ip`도 `.151`인지 확인해야 실제 source IP 분리가 검증된다.

## 14. 전체 검증

media와 clients가 모두 준비된 뒤 실행한다.

```bash
bash scripts/run.sh playbooks/04_verify.yml
```

검증 내용:

- VOD/LIVE 수량과 1080p/720p
- HLS directory와 PostgreSQL catalog 일치
- LIVE playlist 전진
- 필수 platform container 실행
- Pi마다 logical client 10개 실행
- 일곱 health endpoint 응답

이 playbook은 정상/공격 시청 traffic과 model 학습을 실행하지 않는다.

## 15. logical client만 정지

```bash
bash scripts/run.sh playbooks/90_stop_clients.yml
```

Client Pi의 logical client container만 내린다. 서버, DB, Edge, Origin media는 유지한다.

## 16. 파일별 역할

| 파일 | 역할 |
|---|---|
| `scripts/install_control_node.sh` | `ott-control`에 Ansible 전용 Python 환경 설치 |
| `scripts/run.sh` | 지정한 playbook을 정해진 config/inventory로 실행 |
| `scripts/ansible.sh` | ad-hoc Ansible module 실행 |
| `scripts/bootstrap_node.sh` | 한 대 수동 준비용 예비 script, 일반 구축에서는 사용하지 않음 |
| `playbooks/00_bootstrap_fresh_os.yml` | 17대 Docker와 `ottadmin` 설치 |
| `playbooks/00_preflight.yml` | OS, hostname, IP, Docker, NTP, `eth0` 검사 |
| `playbooks/01_sync_repository.yml` | 동일 Git SHA 배포 |
| `playbooks/02_deploy_platform.yml` | Storage, Origin, Edge, Processing 실행 |
| `playbooks/03_deploy_clients.yml` | Pi마다 logical client 10개 실행 |
| `playbooks/04_verify.yml` | media, DB, LIVE, container, endpoint 검증 |
| `playbooks/90_stop_clients.yml` | logical client 정지 |

## 17. 자주 발생하는 오류

| 메시지 | 처리 |
|---|---|
| `No module named venv` | `sudo apt install -y python3-venv` |
| `Permission denied`로 `./scripts/run.sh` 실패 | `bash scripts/run.sh ...`로 실행 |
| `Host key verification failed` | 해당 IP에 `ssh-keygen -R IP`, 그다음 직접 SSH |
| `UNREACHABLE` | 장비 전원, Ethernet, IP, SSH 확인 |
| `Permission denied (publickey)` | key 파일과 bootstrap 결과 확인 |
| `docker: permission denied` | bootstrap 재실행 후 새 SSH session 사용 |
| `NTPSynchronized=no` | DNS, gateway, NTP 수정 |
| repository 인증 실패 | public read 또는 17대 deploy credential 설정 |
| `inventory/lab.yml` 없음 | 최신 commit을 pull한 뒤 repository 동기화 재실행 |
| media baseline 실패 | VOD/LIVE 수량, rendition, DB catalog 확인 |
| IP pool overlap/충돌 | DHCP에서 `.151~.250` 제외 |

`docker compose down -v`는 DB와 volume을 삭제하므로 명확한 데이터 초기화 목적이
아니면 실행하지 않는다.
