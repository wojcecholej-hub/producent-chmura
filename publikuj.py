"""Producent – Instagram bez włączonego komputera (GitHub Actions, uruchamiane co 5 minut).

Instagram nie ma planowania w API, więc o terminie ktoś musi zawołać media_publish: robi to ten skrypt na serwerze
GitHuba. Repozytorium jest publiczne (darmowe minuty bez limitu), dlatego wszystko, co niesie treść, jest zaszyfrowane
kluczem z sekretów (AES-256, openssl): wpisy kolejki (kolejka/<id>.enc) i pliki w wydaniu „pliki” (<id>.mp4.enc,
<id>.jpg.enc). Jawny jest tylko stan.json (numer, stan, link do posta).

Zasada nadrzędna (jak w wersji Cloudflare): lepiej nie opublikować i zgłosić błąd niż opublikować dwa razy.
Stan „publikuje” trafia do repozytorium (push) ZANIM padnie media_publish; push musi przejść bez scalania, więc
równoległe „anuluj” z laptopa wygrywa albo widać, że już za późno.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

PRZED_S = 25 * 60        # ten przebieg bierze posty z terminem w ciągu 25 min (cron GitHuba bywa spóźniony)
LIMIT_KONTENERA_S = 30 * 60
ZAWIESZONE_S = 40 * 60   # „tworzy” bez postępu (przebieg zabity) → można zacząć od nowa
TAG = "pliki"
TU = Path.cwd()
STAN = TU / "stan.json"
REPO = os.environ.get("GITHUB_REPOSITORY", "")
teraz = lambda: int(time.time())  # noqa: E731


def log(t: str) -> None:
    print(time.strftime("%H:%M:%S"), t, flush=True)


def sh(*cmd: str, wejscie: bytes | None = None, sprawdz: bool = True) -> str:
    r = subprocess.run(cmd, input=wejscie, capture_output=True)
    if sprawdz and r.returncode:
        raise RuntimeError(f"{' '.join(cmd[:3])}: {r.stderr.decode(errors='replace')[-400:]}")
    return r.stdout.decode(errors="replace")


def odszyfruj(dane: bytes) -> bytes:
    return subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "100000", "-pass", "env:KLUCZ"],
                          input=dane, capture_output=True, check=True).stdout


# --- repozytorium: stan i znaczniki -------------------------------------------------------------------------------

def wczytaj_stan() -> dict:
    try:
        return json.loads(STAN.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"stany": {}}


def pull() -> None:
    sh("git", "pull", "--rebase", "-q", "origin", "HEAD")


def zapisz(stan: dict, opis: str, scisle: bool = False) -> bool:
    """Commit + push stan.json. scisle=True: push musi przejść jako pierwszy (bez scalania) – inaczej False
    i wołający sprawdza na nowo (np. czy w międzyczasie nie przyszło „anuluj”)."""
    STAN.write_text(json.dumps(stan, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (TU / "README.md").write_text(tablica(stan), encoding="utf-8")
    sh("git", "add", "-A")
    if not sh("git", "status", "--porcelain").strip():
        return True
    sh("git", "commit", "-q", "-m", opis)
    for _ in range(5):
        r = subprocess.run(["git", "push", "-q", "origin", "HEAD"], capture_output=True)
        if r.returncode == 0:
            return True
        if scisle:
            sh("git", "reset", "-q", "--hard", "HEAD~1")
            pull()
            return False
        pull()
    raise RuntimeError("git push nie przeszedł 5 razy")


def anulowany(id_: str) -> bool:
    return (TU / "anuluj" / id_).exists()


def usun_pliki(id_: str) -> None:
    for nazwa in (f"{id_}.mp4.enc", f"{id_}.jpg.enc"):
        subprocess.run(["gh", "release", "delete-asset", TAG, nazwa, "-y", "-R", REPO], capture_output=True)
    for p in (TU / "kolejka" / f"{id_}.enc", TU / "anuluj" / id_):
        if p.exists():
            p.unlink()


# --- Graph API -----------------------------------------------------------------------------------------------------

class Blad(Exception):
    def __init__(self, tekst: str, pewny: bool = True, kod: int | None = None):
        super().__init__(tekst)
        self.pewny, self.kod = pewny, kod


def graph(metoda: str, wersja: str, sciezka: str, token: str, **pola) -> dict:
    adres = f"https://graph.facebook.com/{wersja}/{sciezka}"
    try:
        if metoda == "GET":
            odp = requests.get(adres, params={**pola, "access_token": token}, timeout=60)
        else:
            pliki = pola.pop("_pliki", None)
            odp = requests.post(adres, data={**pola, "access_token": token}, files=pliki, timeout=300)
    except requests.RequestException as e:
        raise Blad(f"brak połączenia ({type(e).__name__})", pewny=False) from None
    try:
        dane = odp.json()
    except ValueError:
        dane = {}
    if odp.status_code != 200 or "error" in dane:
        e = dane.get("error") or {}
        kod = "/".join(str(x) for x in (e.get("code"), e.get("error_subcode")) if x)
        raise Blad(f"{e.get('error_user_msg') or e.get('message') or 'HTTP ' + str(odp.status_code)}{f' (kod {kod})' if kod else ''}",
                   pewny=odp.status_code < 500, kod=e.get("code"))
    return dane


PUBLICZNE = "publiczne"  # wydanie na pliki wystawiane na kilka minut, żeby Instagram je pobrał (video_url, cover_url)


def wystaw(plik: Path) -> str:
    """Odszyfrowany plik pod losową nazwą w publicznym wydaniu; adres do pobrania dla Instagrama."""
    if subprocess.run(["gh", "release", "view", PUBLICZNE, "-R", REPO], capture_output=True).returncode:
        subprocess.run(["gh", "release", "create", PUBLICZNE, "-R", REPO, "--title", PUBLICZNE,
                        "--notes", "Pliki na kilka minut przed publikacją (Instagram pobiera je przez adres)."], capture_output=True)
    sh("gh", "release", "upload", PUBLICZNE, str(plik), "--clobber", "-R", REPO)
    return f"https://github.com/{REPO}/releases/download/{PUBLICZNE}/{plik.name}"


def schowaj(nazwy: list[str]) -> None:
    for n in nazwy:
        subprocess.run(["gh", "release", "delete-asset", PUBLICZNE, n, "-y", "-R", REPO], capture_output=True)


def przygotuj_kontener(w: dict, token: str) -> tuple[str, bool]:
    """Plik z wydania → odszyfrowanie → publiczny adres na kilka minut → kontener z video_url → FINISHED → plik znika.
    18.11.2026: wgrywanie bezpośrednie (rupload) psuje się przy rolkach dłuższych niż ok. 30 s (ProcessingFailedError),
    pobieranie przez adres działa. Nic nie publikuje."""
    import secrets
    import tempfile
    katalog = Path(tempfile.mkdtemp(prefix="pobrane-"))  # poza repozytorium: pliki nie mogą trafić do commita
    sh("gh", "release", "download", TAG, "-p", f"{w['id']}.*", "-D", str(katalog), "--clobber", "-R", REPO)
    los = secrets.token_hex(16)
    mp4 = katalog / f"{los}.mp4"
    mp4.write_bytes(odszyfruj((katalog / f"{w['id']}.mp4.enc").read_bytes()))
    wystawione = [mp4.name]
    try:
        pola = {"media_type": "REELS", "video_url": wystaw(mp4), "caption": w["opis"], "share_to_feed": "true",
                **(w.get("dodatki") or {})}
        if (katalog / f"{w['id']}.jpg.enc").exists():
            jpg = katalog / f"{los}.jpg"
            jpg.write_bytes(odszyfruj((katalog / f"{w['id']}.jpg.enc").read_bytes()))
            pola["cover_url"] = wystaw(jpg)
            wystawione.append(jpg.name)
        for proba in range(1, 4):
            try:
                k = graph("POST", w["wersja"], f"{w['ig']}/media", token, **pola)
            except Blad as e:
                if "cover_url" not in pola or not e.pewny:
                    raise
                log(f"Instagram nie przyjął okładki ({e}) – bez niej")
                pola.pop("cover_url")
                k = graph("POST", w["wersja"], f"{w['ig']}/media", token, **pola)
            log(f"kontener {k['id']} (próba {proba}), Instagram pobiera plik")
            start = teraz()
            while True:
                s = graph("GET", w["wersja"], k["id"], token, fields="status_code,status")
                kod = s.get("status_code")
                if kod == "FINISHED":
                    return k["id"], "cover_url" in pola
                if kod in ("ERROR", "EXPIRED"):
                    log(f"Instagram: {kod} {s.get('status', '')}")
                    break
                if teraz() - start > LIMIT_KONTENERA_S:
                    raise Blad("Instagram nie przetworzył wideo w 30 minut. Post nie powstał.")
                time.sleep(15)
            time.sleep(20)
        raise Blad(f"Instagram 3 razy odrzucił materiał: {s.get('status', '')}")
    finally:
        schowaj(wystawione)


def opublikuj(w: dict, kontener: str, token: str) -> dict:
    for proba in range(12):
        try:
            p = graph("POST", w["wersja"], f"{w['ig']}/media_publish", token, creation_id=kontener)
            break
        except Blad as e:
            if e.kod == 9007 and proba < 11:  # materiał jeszcze niegotowy – post na pewno nie powstał
                time.sleep(20)
                continue
            raise
    wynik = {"stan": "opublikowane", "media": p["id"], "link": "", "uwaga": "", "blad": ""}
    try:
        wynik["link"] = graph("GET", w["wersja"], p["id"], token, fields="permalink").get("permalink", "")
    except Blad:
        pass
    if w.get("komentarz"):
        try:
            graph("POST", w["wersja"], f"{p['id']}/comments", token, message=w["komentarz"])
        except Blad as e:
            wynik["uwaga"] = f"Pierwszy komentarz nie dodany ({e}), dodaj go ręcznie."
    return wynik


# --- obrót --------------------------------------------------------------------------------------------------------

def wpisy() -> dict[str, dict]:
    wynik = {}
    for p in sorted((TU / "kolejka").glob("*.enc")):
        try:
            w = json.loads(odszyfruj(base64.b64decode(p.read_bytes())))
            wynik[p.stem] = {**w, "id": p.stem}
        except Exception as e:  # noqa: BLE001
            log(f"{p.name}: nie da się odczytać ({e})")
    return wynik


MAKS_PROB = 6  # przejściowe błędy (sieć, 5xx) – potem „błąd” i alarm


def juz_jest(w: dict, token: str) -> dict | None:
    """Czy rolka z tym opisem już jest na Instagramie (po przerwanej publikacji). Porównanie początku opisu."""
    try:
        d = graph("GET", w["wersja"], f"{w['ig']}/media", token, fields="id,caption,permalink,timestamp", limit="15")
    except Blad:
        return None
    poczatek = (w.get("opis") or "").strip()[:80]
    for m in d.get("data", []):
        if poczatek and (m.get("caption") or "").strip()[:80] == poczatek:
            return {"stan": "opublikowane", "media": m["id"], "link": m.get("permalink", ""), "blad": "",
                    "uwaga": "Publikacja była przerwana, ale post jest na Instagramie (sprawdzone)."}
    return None


ZLE = {"blad": "błąd", "niepewne": "niepewne", "spoznione": "spóźnione"}
NAZWY_STANOW = {"czeka": "czeka", "tworzy": "przygotowuję", "kontener": "gotowa, czeka na godzinę", "publikuje": "publikuję",
                "opublikowane": "opublikowana", "anulowane": "anulowana", "proba-ok": "próba OK", **ZLE}


def alarmy(stan: dict, W: dict) -> None:
    """Rolka, która nie wyszła, zakłada zgłoszenie (Issue): GitHub wysyła wtedy mail i powiadomienie na telefon.
    Bez treści posta (repozytorium jest publiczne) – tylko termin, numer i przyczyna."""
    for id_, s in stan.get("stany", {}).items():  # tylko wpisy z kolejki (wpis usunięty ręcznie albo próbny – bez alarmu)
        if s.get("stan") in ZLE and not s.get("zgloszone") and id_ in W and not W[id_].get("proba"):
            kiedy = time.strftime("%d.%m %H:%M", time.localtime(s.get("kiedy", 0)))
            r = subprocess.run(["gh", "issue", "create", "-R", REPO, "--title", f"Rolka na {kiedy} nie wyszła ({ZLE[s['stan']]})",
                                "--body", f"Numer: {id_}\n\n{s.get('blad', '')}\n\nCo zrobić: otwórz Producenta → Kalendarz; "
                                          "stan tej pozycji jest już przy niej. Po poprawce zamknij to zgłoszenie."],
                               capture_output=True)
            if r.returncode == 0:
                s["zgloszone"] = True


def tablica(stan: dict) -> str:
    """README.md: czytelna z telefonu lista – termin, stan, link (bez tytułów i opisów)."""
    wiersze = sorted(stan.get("stany", {}).items(), key=lambda x: x[1].get("kiedy", 0))
    teraz_ = teraz()
    W = wpisy()  # w tablicy: to, co jest w kolejce, i to, co wyszło w ostatnich 2 dniach (bez prób)
    przyszle = [(i, s) for i, s in wiersze if s.get("kiedy", 0) > teraz_ - 2 * 86400 and s.get("stan") != "proba-ok"
                and ((i in W and not W[i].get("proba")) or s.get("stan") == "opublikowane")]
    tekst = ["# Producent – kolejka Instagrama", "",
             "Automat publikuje zaplanowane rolki o terminie (laptop może być wyłączony). Treść kolejki jest zaszyfrowana.", "",
             f"Odświeżone: {time.strftime('%d.%m.%Y %H:%M')}", "", "| Termin | Stan | Post |", "|---|---|---|"]
    for i, s in przyszle:
        znak = "⚠️ " if s.get("stan") in ZLE else ("✅ " if s.get("stan") == "opublikowane" else "")
        link = f"[otwórz]({s['link']})" if s.get("link") else ""
        lt = time.localtime(s.get('kiedy', 0))
        tekst.append(f"| {('pon', 'wt', 'śr', 'czw', 'pt', 'sob', 'nd')[lt.tm_wday]} {time.strftime('%d.%m %H:%M', lt)} | {znak}{NAZWY_STANOW.get(s.get('stan'), s.get('stan'))} | {link} |")
    return "\n".join(tekst) + "\n"


def obrot(start_przebiegu: bool = False) -> None:
    """start_przebiegu=True: działa tylko ten przebieg (concurrency), więc „tworzy”, „kontener” i „publikuje”
    zostały po martwym poprzedniku – od razu, bez czekania 40 minut."""
    tokeny = json.loads(os.environ.get("META_TOKENY") or "{}")
    stan = wczytaj_stan()
    S = stan.setdefault("stany", {})
    t = teraz()
    prog = 0 if start_przebiegu else ZAWIESZONE_S
    W = wpisy()
    for id_, w in W.items():
        s = S.setdefault(id_, {"stan": "czeka", "kiedy": w["kiedy"], "zmieniono": t})
        if s["stan"] in ("czeka", "kontener") and anulowany(id_):
            s.update(stan="anulowane", zmieniono=t)
            usun_pliki(id_)
        elif s["stan"] in ("tworzy", "kontener") and t - s.get("zmieniono", t) > prog and not (s["stan"] == "kontener" and not start_przebiegu):
            s.update(stan="czeka", zmieniono=t)  # kontener bez publikacji można założyć od nowa – post na pewno nie powstał
        elif s["stan"] == "publikuje" and t - s.get("zmieniono", t) > prog:
            s.update(stan="niepewne", blad="Publikacja przerwana w połowie. Sprawdź na Instagramie, czy post powstał.", zmieniono=t)
        elif s["stan"] == "czeka" and t > w["kiedy"] + w.get("limit_s", 43200):
            s.update(stan="spoznione", blad="Termin minął, a serwer GitHuba nie zdołał opublikować w dozwolonym czasie.", zmieniono=t)
            usun_pliki(id_)
        if s["stan"] == "niepewne" and not s.get("sprawdzone") and tokeny.get(w["konto"]):
            s["sprawdzone"] = True
            if jest := juz_jest(w, tokeny[w["konto"]]):
                s.update(**jest, zmieniono=t)
                usun_pliki(id_)
    alarmy(stan, W)
    zapisz(stan, "obrót: porządki")

    for id_, w in sorted(wpisy().items(), key=lambda x: x[1]["kiedy"]):
        s = S.get(id_) or {}
        if s.get("stan") != "czeka" or (w["kiedy"] > teraz() + PRZED_S and not w.get("proba")):
            continue
        token = tokeny.get(w["konto"], "")
        if not token:
            s.update(stan="blad", blad=f"Brak tokenu Meta w sekretach GitHuba dla konta {w['konto']}.", zmieniono=teraz())
            zapisz(stan, f"{id_}: brak tokenu")
            continue
        s.update(stan="tworzy", zmieniono=teraz())
        zapisz(stan, f"{id_}: tworzy")
        log(f"{id_}: przygotowuję (termin {time.strftime('%d.%m %H:%M', time.localtime(w['kiedy']))})")
        try:
            kontener, z_okladka = przygotuj_kontener(w, token)
        except Exception as e:  # noqa: BLE001
            proby = s.get("proby", 0) + 1
            pewny = isinstance(e, Blad) and e.pewny
            if isinstance(e, Blad) and e.kod == 190:
                tekst = "Token Meta wygasł albo został cofnięty – w Producencie: Połączenia → połącz konto ponownie."
            else:
                tekst = f"Instagram nie przyjął rolki: {e}"[:300]
            s.update(stan="blad" if pewny or proby >= MAKS_PROB else "czeka", proby=proby, blad=tekst, zmieniono=teraz())
            alarmy(stan, wpisy())
            zapisz(stan, f"{id_}: błąd kontenera")
            continue
        if w.get("proba"):  # wpis próbny: sprawdzamy całą drogę do gotowego kontenera, ale NIGDY nie publikujemy
            s.update(stan="proba-ok", kontener=kontener, uwaga="z okładką" if z_okladka else "bez okładki", blad="", zmieniono=teraz())
            usun_pliki(id_)
            zapisz(stan, f"{id_}: próba OK")
            continue
        s.update(stan="kontener", kontener=kontener, zmieniono=teraz())
        zapisz(stan, f"{id_}: kontener gotowy")
        while teraz() < w["kiedy"]:  # czekamy na minutę publikacji; co minutę sprawdzamy, czy nie przyszło „anuluj”
            time.sleep(min(60, max(1, w["kiedy"] - teraz())))
            pull()
            if anulowany(id_):
                break
        # Od tego miejsca brak pewności = brak ponowienia.
        while True:
            pull()
            stan = wczytaj_stan()
            S = stan["stany"]
            s = S[id_]
            if anulowany(id_) or s.get("stan") != "kontener":
                s.update(stan="anulowane", zmieniono=teraz())
                usun_pliki(id_)
                zapisz(stan, f"{id_}: anulowane")
                break
            s.update(stan="publikuje", zmieniono=teraz())
            if zapisz(stan, f"{id_}: publikuje", scisle=True):
                break
        if s.get("stan") != "publikuje":
            continue
        try:
            s.update(**opublikuj(w, kontener, token), zmieniono=teraz())
            usun_pliki(id_)
        except Blad as e:
            s.update(stan="niepewne" if not e.pewny else "blad", zmieniono=teraz(),
                     blad=f"Instagram nie opublikował: {e}" + ("" if e.pewny else " Sprawdź na Instagramie, czy post powstał."))
        log(f"{id_}: {s['stan']} {s.get('link', '')}")
        alarmy(stan, wpisy())
        zapisz(stan, f"{id_}: {s['stan']}")


CZAS_BIEGU_S = 5 * 3600 + 30 * 60  # GitHub zabija przebieg po 6 h – wcześniej przekazujemy pałeczkę


def nastepny_termin() -> int | None:
    """Najbliższy termin wpisu, który jeszcze czeka (None = kolejka pusta)."""
    S = wczytaj_stan().get("stany", {})
    terminy = [w["kiedy"] for i, w in wpisy().items() if (S.get(i) or {}).get("stan", "czeka") == "czeka" and not w.get("proba")]
    return min(terminy) if terminy else None


def czuwaj(start: int) -> None:
    """Zegar GitHuba (cron) bywa zawodny – 18.11 nie ruszył przez 6 h. Dopóki w kolejce coś czeka, przebieg sam
    doczekuje do kolejnego terminu (budząc się co 10 min po nowe wpisy), a przed limitem 6 h uruchamia następcę."""
    while True:
        t = nastepny_termin()
        if t is None:
            log("kolejka pusta – koniec (obudzi mnie nowy plan z Producenta albo cron)")
            return
        if teraz() - start > CZAS_BIEGU_S:
            log("limit czasu przebiegu – następcę uruchomi krok „Następca”")
            return
        czekaj = t - PRZED_S + 60 - teraz()
        if czekaj > 0:
            log(f"następny termin {time.strftime('%d.%m %H:%M', time.localtime(t))} – czekam")
            time.sleep(min(czekaj, 600))
            pull()
            if czekaj > 600:
                continue
        obrot()
        time.sleep(30)  # błąd przejściowy zostawia wpis „czeka” – bez tej pauzy pętla kręciłaby się bez przerwy


if __name__ == "__main__":
    if not os.environ.get("KLUCZ"):
        sys.exit("Brak sekretu KLUCZ.")
    if sys.argv[1:] == ["nastepca"]:  # krok „always”: po końcu, awarii albo limicie czasu – kolejny przebieg, gdy coś czeka
        if nastepny_termin() is not None:
            # bezpiecznik: przebieg, który padł w pierwszych minutach, nie startuje następcy w kółko – zgłoszenie zamiast pętli
            r = subprocess.run(["gh", "run", "view", os.environ.get("GITHUB_RUN_ID", ""), "-R", REPO, "--json", "createdAt",
                                "-q", ".createdAt"], capture_output=True, text=True)
            try:
                import calendar
                trwal = teraz() - calendar.timegm(time.strptime(r.stdout.strip(), "%Y-%m-%dT%H:%M:%SZ"))
            except ValueError:
                trwal = 9999
            if trwal < 240:
                tytul = "Automat pada zaraz po starcie"
                lista = subprocess.run(["gh", "issue", "list", "-R", REPO, "--state", "open", "--search", tytul, "--json", "number"],
                                       capture_output=True, text=True).stdout
                if lista.strip() in ("", "[]"):
                    subprocess.run(["gh", "issue", "create", "-R", REPO, "--title", tytul, "--body",
                                    "Przebieg skończył się w ciągu kilku minut, a w kolejce coś czeka. Następca NIE został uruchomiony, "
                                    "żeby nie kręcić się w kółko. Zapasowo ruszy cron albo plan z Producenta. Szczegóły: zakładka Actions."],
                                   capture_output=True)
                log("przebieg zbyt krótki – bez następcy (zgłoszenie)")
            else:
                subprocess.run(["gh", "workflow", "run", "publikuj.yml", "-R", REPO], capture_output=True)
                log("w kolejce coś czeka – uruchomiony następca")
        sys.exit(0)
    start = teraz()
    obrot(start_przebiegu=True)
    czuwaj(start)
