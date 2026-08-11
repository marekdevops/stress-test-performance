# Wdrożenie w środowisku bez dostępu do internetu

Ten dokument jest samowystarczalny — nie wymaga niczego poza zawartością tego
repo i jednego pliku z obrazem.

## Gotowy obraz jest w repo

**Obrazu nie da się zbudować po stronie offline** — `Containerfile` ciąga
`sysbench` z EPEL9, a `python3` z repozytoriów UBI, jedno i drugie wymaga
egressu. Dlatego zbudowany obraz leży w katalogu `image/`, w kawałkach po 90 MB
(GitHub twardo odrzuca pojedyncze pliki powyżej 100 MB). Sklejasz je jedną
komendą, bez internetu i bez dodatkowych narzędzi — wystarczy `cat` i `sha256sum`.

Do przeniesienia przez granicę sieci jest więc **tylko repo**.

---

## Faza 1 — pobranie repo (maszyna Z dostępem do internetu)

```bash
git clone https://github.com/marekdevops/stress-test-performance.git
```

Jeśli maszyna offline nie ma dostępu do GitHuba (a zwykle nie ma), spakuj repo
do jednego pliku razem z całą zawartością `image/`:

```bash
cd stress-test-performance
git bundle create ../stress-test-performance.bundle --all
```

Przenosisz jeden plik `.bundle` (ok. 55 MB) — nośnikiem zgodnym z Waszą
procedurą przenoszenia danych do strefy offline.

---

## Faza 2 — maszyna offline: odtworzenie obrazu

```bash
git clone stress-test-performance.bundle stress-test-performance   # jeśli bundle
cd stress-test-performance

./build.sh unpack          # dodaj SUDO=sudo, jeśli docker wymaga roota
```

`unpack` weryfikuje sumy SHA256 każdego kawałka, skleja je, sprawdza sumę
całego archiwum i importuje obraz do lokalnego dockera/podmana. Jeśli transfer
był niepełny, zatrzyma się z błędem zamiast zaimportować uszkodzony obraz.

Sprawdź, że obraz jest na miejscu:

```bash
docker images | grep sysbench-perf
```

### Aktualizacja obrazu (strona z internetem)

Po zmianie kodu przepakuj i zacommituj `image/`:

```bash
SUDO=sudo ./build.sh build
SUDO=sudo ./build.sh package
git add image && git commit -m "Rebuild image" && git push
```

---

## Faza 3 — wepchnięcie obrazu do rejestru klastra

```bash
oc new-project perf-test
SUDO=sudo NAMESPACE=perf-test ./build.sh push
```

`push` sam wybiera drogę, w tej kolejności:

1. **`REGISTRY=...`** — jeśli podasz firmowy rejestr (Quay/Harbor/Artifactory),
   idzie prosto tam. W środowisku disconnected to zwykle najlepsza opcja, bo taki
   rejestr i tak już tam jest — z niego instalowano klaster:
   ```bash
   REGISTRY=rejestr.firma.local SUDO=sudo NAMESPACE=perf-test ./build.sh push
   ```
2. **route do wewnętrznego rejestru** — jeśli ktoś go wystawił (patrz niżej).
3. **tunel `oc port-forward`** — automatyczny fallback, **nie wymaga żadnej
   zmiany w klastrze**.

### Tunel — gdy nie ma route'a (najczęstszy przypadek w strefie zamkniętej)

Komunikat `the internal registry has no external route` w starszej wersji
skryptu oznaczał ślepy zaułek. Teraz `push` sam zestawia tunel do
`svc/image-registry`, pushuje przez `127.0.0.1` i tunel zamyka. Nie trzeba
wystawiać route'a, ruszać konfiguracji rejestru ani zaufania do CA — docker
traktuje `127.0.0.0/8` jako rejestr insecure, a podman dostaje
`--tls-verify=false` automatycznie.

Wymagane uprawnienie (sprawdź, jeśli push się nie uda):

```bash
oc auth can-i create pods/portforward -n openshift-image-registry
```

Port dobierany jest automatycznie od 5000 w górę, gdyby był zajęty; można go
narzucić przez `LOCAL_PORT=5050`. Tę ścieżkę można też wymusić ręcznie, nawet
gdy route istnieje:

```bash
SUDO=sudo NAMESPACE=perf-test ./build.sh push-local
```

### Jeśli route jednak istnieje

Certyfikat jest zwykle self-signed, więc najpierw zaufaj jego CA (jednorazowo,
na maszynie z której pushujesz):

```bash
REG=$(oc get route default-route -n openshift-image-registry -o jsonpath='{.spec.host}')

# CA wymaga uprawnień cluster-admin do odczytu:
oc get secret router-ca -n openshift-ingress-operator \
  -o jsonpath='{.data.tls\.crt}' | base64 -d > ca.crt

sudo mkdir -p /etc/docker/certs.d/$REG
sudo cp ca.crt /etc/docker/certs.d/$REG/ca.crt
```

Docker czyta `certs.d` przy każdym pushu — restart demona nie jest potrzebny.

### Wystawienie route'a (opcjonalne, wymaga change managementu)

Tunel z poprzedniej sekcji czyni to zbędnym, ale gdyby route był potrzebny
na stałe — to realna zmiana w klastrze, cluster-admin:

```bash
oc patch configs.imageregistry.operator.openshift.io/cluster --type=merge \
  -p '{"spec":{"defaultRoute":true}}'
```

### Jeszcze inne drogi

* **skopeo, prosto z archiwum do dowolnego rejestru**, bez pośrednictwa
  lokalnego demona (archiwum powstaje w `dist/` po `./build.sh unpack`):

  ```bash
  skopeo copy --dest-tls-verify=false \
    docker-archive:sysbench-perf-latest.tar.gz \
    docker://rejestr.firma.local/perf/sysbench-perf:latest
  ```

  Wtedy w kroku wdrożenia podaj `-p IMAGE=rejestr.firma.local/perf/sysbench-perf:latest`.

* **`oc image mirror`** — jeśli macie już ustawiony proces mirrorowania
  dla instalacji disconnected, użyjcie tej samej ścieżki.

---

## Faza 4 — wdrożenie

```bash
oc new-app -f deploy/template.yaml \
  -p IMAGE=image-registry.openshift-image-registry.svc:5000/perf-test/sysbench-perf:latest \
  -p CPU_QUOTA=2
```

Template tworzy wyłącznie `Deployment` + `Service` + `Route` we własnym
projekcie. Nie dotyka niczego na poziomie node'a, kubeleta ani klastra.

Dobierz `CPU_QUOTA` do wolnej pojemności node'a — przy zbyt dużej wartości pod
zostanie `Pending` z `Insufficient cpu`:

```bash
oc describe node <NODE> | grep -A6 "Allocated resources"
```

Zasada: `CPU_QUOTA` = `CPU_THREADS` + 1. Ta dodatkowa jednostka jest dla wątku
serwera HTTP — bez niej odpytywanie UI podgryza rdzeń benchmarku i widać to jako
niezerowy `nr_throttled`.

---

## Faza 5 — weryfikacja

```bash
oc rollout status deploy/sysbench-perf
oc logs -f deploy/sysbench-perf

ROUTE=$(oc get route sysbench-perf -o jsonpath='{.spec.host}')
echo "https://$ROUTE"

curl -k "https://$ROUTE/cpu"
curl -k "https://$ROUTE/memory"
curl -kX POST "https://$ROUTE/run/all"      # powtórka bez restartu poda
```

Route ma certyfikat self-signed — w przeglądarce trzeba raz zaakceptować wyjątek,
w `curl` używać `-k`.

Poprawny start w logach wygląda tak:

```
sysbench: sysbench 1.0.20
node=<nazwa> pod=<nazwa> nproc=4 quota=2.0 cores
AUTORUN enabled - queueing cpu + memory
listening on :8080
```

---

## Wybór node'a do testu

Template nie ustawia `nodeSelector`. Żeby przypiąć test do konkretnej maszyny
(porównanie bare-metal vs node wirtualny):

```bash
oc patch deployment sysbench-perf --type=merge \
  -p '{"spec":{"template":{"spec":{"nodeSelector":{"kubernetes.io/hostname":"NAZWA-NODE"}}}}}'
```

Pod zostanie odtworzony na wskazanym node'zie (strategia `Recreate`), a nazwa
node'a trafi do każdego wyniku przez Downward API.

---

## Eksport wyników z sieci offline

Historia żyje w `emptyDir` i ginie razem z podem. Do archiwum:

```bash
curl -sk "https://$ROUTE/api/results" > wyniki-$(date +%Y%m%d-%H%M).json
```

JSON zawiera komplet: metryki sysbencha, deltę throttlingu CFS, limit cgroup,
`physical_package_id` per vCPU, MHz przed/po oraz `started_at`/`finished_at`
w UTC — czyli okno czasowe do skorelowania z próbkowaniem `psr` i MHz
na fizycznym hoście.

---

## Odinstalowanie

```bash
oc delete all -l app.kubernetes.io/name=sysbench-perf
# lub w całości:
oc delete project perf-test
```

---

## Rozwiązywanie problemów

| Objaw | Przyczyna | Rozwiązanie |
|---|---|---|
| pod `Pending`, `Insufficient cpu` | `CPU_QUOTA` > wolna pojemność node'a | zmniejsz `CPU_QUOTA` |
| `ImagePullBackOff` | obraz nie trafił do rejestru albo zła ścieżka w `IMAGE` | `oc get istag -n <ns>`, sprawdź `IMAGE` |
| `x509: certificate signed by unknown authority` przy pushu | brak CA rejestru | patrz Faza 3, albo użyj `push-local` (tunel omija problem) |
| `the internal registry has no external route` | brak route'a do rejestru | nic nie rób — `push` sam przechodzi na tunel; wymaga prawa `pods/portforward` |
| `the tunnel did not come up` | brak prawa do port-forward | `oc auth can-i create pods/portforward -n openshift-image-registry` |
| `exit 127`, `No such file or directory: 'sysbench'` w wyniku | obraz zbudowany bez EPEL (brak egressu w czasie builda) | zbuduj ponownie na maszynie z internetem |
| `409` przy `POST /run/*` | benchmark już trwa | poczekaj; `GET /api/status` pokazuje stan |
| pod `OOMKilled` | `block_size` większy niż `MEMORY_QUOTA` | podnieś `MEMORY_QUOTA` albo zmniejsz blok |
| niezerowy `nr_throttled` | CFS dławi benchmark | podnieś `CPU_QUOTA` do `CPU_THREADS + 1`; wynik z throttlingiem nie jest miarą sprzętu |
