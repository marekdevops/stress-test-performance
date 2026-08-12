# Tuning CPU przez Node Tuning Operator

Eksperyment: czy profil TuneD z `governor=performance` podnosi wyniki sysbencha.
**Nic tutaj nie zostało jeszcze zastosowane w żadnym klastrze.**

## Co to właściwie jest

W OpenShifcie nie instaluje się osobnego operatora — **Node Tuning Operator
(NTO)** jest częścią instalacji i już zarządza TuneD na każdym node'zie.
Dodanie własnego profilu to utworzenie CR-a `Tuned`, nie wdrożenie operatora.

Nie mylić z **Performance Profile** (`performance.openshift.io/v2`) — to cięższe
narzędzie z tego samego operatora, opisane niżej jako poziom 3.

## Trzy poziomy, rosnąco pod względem ryzyka

| | Co robi | Restart node'a | Plik |
|---|---|---|---|
| **A** | governor, EPB, min_perf_pct, turbo | **nie** | `tuned-cpu-performance.yaml` |
| **B** | A + blokada głębokich stanów C | **nie** | `tuned-cpu-performance-cstate.yaml` |
| **C** | izolacja rdzeni, hugepages, kernel args | **tak** | nie przygotowany świadomie |

Zacznij od A. Poziom C (`PerformanceProfile`) generuje MachineConfig i **restartuje
node'y po kolei** — w środowisku regulowanym to zmiana pod pełny change management,
a dla samego pomiaru sysbencha nie jest potrzebna.

## Gdzie to kierować — kluczowa decyzja

**Na node'y bare-metal, nie na wirtualne node'y OpenShifta.**

Wewnątrz gościa KVM nie ma sterownika cpufreq ani wglądu w p-state hosta —
profil zastosowany na VM nie zrobi nic. Częstotliwością rządzi wyłącznie fizyczny
host i tam trzeba go założyć. To ten sam mechanizm, przez który guest pokazuje
zamrożone `cpu MHz: 1900`.

Zgodnie z ustaleniami: **najpierw mały pool** (`worker-priority`), nie cały
klaster. Etykieta w `recommend.match` to placeholder — sprawdź, jak są oznaczone
Twoje node'y:

```bash
oc get nodes --show-labels
oc get nodes -l node-role.kubernetes.io/worker-priority
```

## Zanim cokolwiek zastosujesz — stan wyjściowy

Być może governor jest już na `performance` po zmianie profilu BIOS na
Performance. Wtedy ten eksperyment nie ma czego poprawić i warto to wiedzieć
przed, a nie po.

```bash
NODE=<nazwa-node-bare-metal>

# jaki profil TuneD jest aktywny
oc debug node/$NODE -q -- chroot /host tuned-adm active

# governor i zakres częstotliwości
oc debug node/$NODE -q -- chroot /host bash -c '
  cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor
  cat /sys/devices/system/cpu/intel_pstate/min_perf_pct
  cat /sys/devices/system/cpu/intel_pstate/no_turbo
  grep -m3 MHz /proc/cpuinfo'

# domyślne priorytety NTO, żeby dobrać własny
oc get tuned default -n openshift-cluster-node-tuning-operator -o yaml
```

## Procedura pomiaru

Sens ma wyłącznie porównanie tej samej maszyny przed i po, w krótkim odstępie.

```bash
ROUTE=$(oc get route sysbench-perf -o jsonpath='{.spec.host}')

# 1. PRZED - trzy przebiegi, żeby znać rozrzut
for i in 1 2 3; do
  curl -kX POST "https://$ROUTE/run/all"
  sleep 90
done
curl -sk "https://$ROUTE/api/results" > przed-tuned.json

# 2. Zastosuj profil
oc apply -f tuned-cpu-performance.yaml

# 3. Poczekaj, aż NTO go rozniesie (bez restartu, kilkadziesiąt sekund)
oc get profile -n openshift-cluster-node-tuning-operator -w
oc debug node/$NODE -q -- chroot /host tuned-adm active   # ma pokazać cpu-performance-bench

# 4. PO - te same trzy przebiegi
for i in 1 2 3; do
  curl -kX POST "https://$ROUTE/run/all"
  sleep 90
done
curl -sk "https://$ROUTE/api/results" > po-tuned.json
```

Porównanie:

```bash
jq -r '.history[] | select(.kind=="cpu") | "\(.started_at) \(.metrics.events_per_second) ev/s throttled=\(.throttling.nr_throttled)"' przed-tuned.json po-tuned.json
```

Rozrzut między przebiegami na tej samej maszynie wynosił u nas ~3%. **Różnica
poniżej tego progu to szum, nie efekt.** Trzy przebiegi po każdej stronie to
minimum, żeby to rozstrzygnąć.

Równolegle, na fizycznym hoście, w tym samym oknie czasowym:

```bash
oc debug node/$NODE -q -- chroot /host bash -c 'while true; do grep -m1 MHz /proc/cpuinfo; sleep 2; done'
```

Timestampy `started_at`/`finished_at` z wyniku sysbencha pozwalają skorelować
jedno z drugim.

## Czego się spodziewać

Uczciwie: **jeśli BIOS jest już w profilu Performance i governor pokazuje
`performance`, wariant A prawdopodobnie nie zmieni nic.** Ustawia to samo, co
już jest ustawione. Wartość eksperymentu polega wtedy na wykluczeniu hipotezy,
a nie na przyspieszeniu — i to też jest wynik wart zapisania.

Realny zysk jest najbardziej prawdopodobny, gdy stan wyjściowy pokazuje
governor `powersave` albo `min_perf_pct` znacząco poniżej 100.

Wariant B może wynik **pogorszyć** — uzasadnienie w komentarzu w samym pliku.
Mierz go osobno.

## Wycofanie

```bash
oc delete -f tuned-cpu-performance.yaml
```

NTO wraca do poprzedniego profilu w kilkadziesiąt sekund, bez restartu node'a.
Potwierdzenie: `tuned-adm active` znów pokaże `openshift-node`.

## Poziom 3 — PerformanceProfile

Świadomie nie przygotowany jako gotowy plik. Daje izolację rdzeni
(`isolated`/`reserved`), hugepages, `irqbalance` i kernel args typu
`intel_idle.max_cstate=0` — ale **restartuje node'y** i trwale rezerwuje
rdzenie pod workload.

Jest natomiast bezpośrednio powiązany z otwartym pytaniem o
`dedicatedCpuPlacement`: to on tworzy warunki (CPU Manager `static`,
etykieta `cpumanager=true`), bez których pinning 1:1 vCPU:pCPU dla VM się nie
włączy. Jeśli celem jest ostatecznie pinning, a nie tylko częstotliwość — to
jest właściwa ścieżka i wtedy warto ją zaplanować osobno.

## Zastrzeżenie

Te manifesty **nie zostały zwalidowane server-side** — klaster CRC, którym
dysponuję, nie ma zainstalowanego NTO (`tuned.openshift.io` nie istnieje).
Przed zastosowaniem sprawdź je na docelowym klastrze:

```bash
oc apply --dry-run=server -f tuned-cpu-performance.yaml
```
