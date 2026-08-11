# sysbench-perf

Pod na OpenShift, który uruchamia `sysbench cpu` i `sysbench memory`, i wystawia
wyniki pod route'em (HTML + tekst + JSON). Test wykonuje się raz przy starcie
i można go powtarzać dowolną liczbę razy bez restartu poda.

```
sysbench cpu    --cpu-max-prime=20000 --threads=1 run
sysbench memory --memory-block-size=1M --memory-total-size=100G --time=0 run
```

## Dlaczego serwer HTTP jest procesem głównym

Pod, w którym PID 1 to sam sysbench, wykonuje test **dokładnie raz** i kończy
się — pod `Deployment` oznacza to `CrashLoopBackOff`, a wynik znika przy
restarcie. Tutaj PID 1 to mały serwer stdlib-owy, a sysbench jest procesem
potomnym: pod żyje, wyniki są serwowane, a test można wywołać ponownie.

Benchmark leci w osobnym wątku, więc liveness/readiness probe odpowiadają
w trakcie pomiaru. Globalny lock dopuszcza tylko jeden przebieg naraz —
dwa równoległe sysbenche mierzyłyby własną kontencję, nie sprzęt.

## Wdrożenie

> **Środowisko bez dostępu do internetu:** gotowy obraz leży w repo (`image/`),
> więc wystarczy `git clone` + `./build.sh unpack`. Pełna procedura krok po kroku
> w [`docs/AIRGAP.md`](docs/AIRGAP.md).

Obraz budujemy lokalnie i przenosimy — build nie odbywa się w klastrze.

```bash
./build.sh unpack                # odtwórz obraz z image/ (nie wymaga internetu)
./build.sh push                  # wepchnij do rejestru zalogowanego klastra
```

`push` sam znajduje drogę do rejestru: `REGISTRY=...` (firmowy Quay/Harbor),
route wewnętrznego rejestru, a jeśli route'a nie ma — tunel `oc port-forward`,
który **nie wymaga żadnej zmiany w klastrze**.

Przebudowa obrazu (wymaga internetu — sysbench pochodzi z EPEL9):

```bash
./build.sh build                 # zbuduj lokalnie (docker lub podman)
./build.sh package               # -> image/ w kawałkach po 90 MB, do commitu
```

Gdy docker wymaga roota, dodaj `SUDO=sudo` — elewuje wyłącznie silnik
kontenerowy, `oc` dalej działa z Twoim loginem do klastra.

Wdrożenie — jedna komenda:

```bash
oc new-app -f deploy/template.yaml \
  -p IMAGE=image-registry.openshift-image-registry.svc:5000/openshift/sysbench-perf:latest

oc get route sysbench-perf -o jsonpath='{.spec.host}'
```

Template tworzy `Deployment` + `Service` + `Route`. Nic poza tym — żadnych
zmian na poziomie node'a, klastra ani konfiguracji kubeleta.

Jeśli wolisz gołe manifesty zamiast Template:
`oc process -f deploy/template.yaml -p IMAGE=... -o yaml > plain.yaml`

## Endpointy

| Metoda | Ścieżka | Opis |
|---|---|---|
| GET | `/` | HTML: osobne sekcje CPU i RAM, historia, przyciski |
| GET | `/cpu`, `/memory` | czysty tekst: metryki + surowy output sysbench |
| GET | `/api/results` | JSON: wszystkie przebiegi + kontekst |
| GET | `/api/status` | czy trwa przebieg |
| GET | `/snapshot` | `lscpu` z wnętrza poda |
| POST | `/run/cpu`, `/run/memory`, `/run/all` | uruchom test (409 jeśli inny trwa) |
| GET | `/healthz`, `/readyz` | probes; niezależne od stanu benchmarku |

Parametry można nadpisać per przebieg, bez redeploya:

```bash
ROUTE=$(oc get route sysbench-perf -o jsonpath='{.spec.host}')
curl -kX POST "https://$ROUTE/run/cpu?cpu_max_prime=2000&threads=1"
curl -kX POST "https://$ROUTE/run/cpu?cpu_max_prime=20000&threads=16&time=30"
curl -k "https://$ROUTE/cpu"
```

## Powtarzalność

* **na starcie** — `AUTORUN=true` (domyślnie): jeden przebieg CPU + RAM,
* **na żądanie** — przycisk w UI albo `POST /run/*`, bez limitu,
* **cyklicznie** — opcjonalny `deploy/optional-cronjob.yaml` (co godzinę POST
  na Service).

Historia to ostatnie `HISTORY_MAX` (domyślnie 20) przebiegów, trzymane
w `emptyDir` — przeżywają restart procesu, nie przeżywają restartu poda.
Do trwałego archiwum: `curl /api/results > wynik.json`.

## Parametry Template

| Parametr | Default | Uwagi |
|---|---|---|
| `IMAGE` | — | pełna referencja obrazu |
| `CPU_MAX_PRIME` | `20000` | |
| `CPU_THREADS` | `1` | |
| `CPU_TIME` | *(puste)* | puste = default sysbencha (10 s) |
| `MEMORY_BLOCK_SIZE` | `1M` | |
| `MEMORY_TOTAL_SIZE` | `100G` | |
| `MEMORY_THREADS` | `1` | |
| `MEMORY_TIME` | `0` | patrz niżej |
| `AUTORUN` | `true` | |
| `CPU_QUOTA` | `2` | request **i** limit → QoS Guaranteed |
| `MEMORY_QUOTA` | `1Gi` | test RAM alokuje tylko jeden blok na wątek |

**`MEMORY_TIME=0` jest celowe.** sysbench domyślnie ma `--time=10` i kończy test
memory po 10 sekundach niezależnie od `--memory-total-size`. Przy 100G oznacza
to, że bez `--time=0` przenosiłbyś tyle, ile zdążysz w 10 s, a nie 100 GiB.

**`CPU_QUOTA` — request = limit celowo.** Przy request niższym od limitu CFS
dławi benchmark i mierzysz kwotę cgroup, a nie procesor. Wartość musi być
całkowita, żeby uzyskać QoS Guaranteed. Przy `CPU_THREADS=16` podnieś ją do 16.

## Kontekst zbierany przy każdym przebiegu

Poza metrykami sysbencha każdy wynik zawiera dane potrzebne do korelacji
z pomiarami na fizycznym hoście:

* `spec.nodeName` (Downward API) — który node wirtualny,
* delta `nr_throttled` / `throttled_usec` z `cpu.stat` — **czy CFS dławił test**;
  niezerowa wartość unieważnia wynik jako miarę sprzętu,
* limit CPU widziany z cgroup,
* `physical_package_id` per vCPU — weryfikacja poprawki topologii `sockets`/`cores`,
* min/max `cpu MHz` przed i po przebiegu,
* `started_at` / `finished_at` w UTC — okno do skorelowania z `psr` + MHz
  próbkowanym na fizycznym hoście.

`cpu MHz` w guescie to zwykle zamrożona wartość nominalna (KVM nie wystawia
APERF/MPERF) — jest zapisywana jako kontekst, nie jako pomiar.

## Bezpieczeństwo / zgodność

* obraz nie działa jako root, akceptuje dowolny UID przydzielony przez OpenShift
  (SCC `restricted-v2`), `readOnlyRootFilesystem`, `drop: ALL`, `seccomp RuntimeDefault`,
* aplikacja nie rozmawia z API Kubernetesa i nie ma ServiceAccounta z uprawnieniami,
* jedyne obciążenie CPU to jawnie wywołany benchmark — `AUTORUN=false` wyłącza
  nawet przebieg startowy,
* `replicas: 1` celowo: przy większej liczbie route rozkładałby ruch i każde
  odświeżenie pokazywałoby wynik z innej maszyny.

## Wybór node'a do testu

Template nie ustawia `nodeSelector`. Aby przypiąć test do konkretnego node'a
(np. porównanie bare-metal vs node wirtualny):

```bash
oc patch deployment sysbench-perf --type=merge \
  -p '{"spec":{"template":{"spec":{"nodeSelector":{"kubernetes.io/hostname":"NAZWA-NODE"}}}}}'
```
