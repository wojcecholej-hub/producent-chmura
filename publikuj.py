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


def obrot() -> None:
    tokeny = json.loads(os.environ.get("META_TOKENY") or "{}")
    stan = wczytaj_stan()
    S = stan.setdefault("stany", {})
    t = teraz()
    for id_, w in wpisy().items():
        s = S.setdefault(id_, {"stan": "czeka", "kiedy": w["kiedy"], "zmieniono": t})
        if s["stan"] in ("czeka", "kontener") and anulowany(id_):
            s.update(stan="anulowane", zmieniono=t)
            usun_pliki(id_)
        elif s["stan"] == "tworzy" and t - s.get("zmieniono", t) > ZAWIESZONE_S:
            s.update(stan="czeka", zmieniono=t)
        elif s["stan"] == "publikuje" and t - s.get("zmieniono", t) > ZAWIESZONE_S:
            s.update(stan="niepewne", blad="Publikacja przerwana w połowie. Sprawdź na Instagramie, czy post powstał.", zmieniono=t)
        elif s["stan"] == "czeka" and t > w["kiedy"] + w.get("limit_s", 43200):
            s.update(stan="spoznione", blad="Termin minął, a serwer GitHuba nie zdołał opublikować w dozwolonym czasie.", zmieniono=t)
            usun_pliki(id_)
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
        except Blad as e:
            s.update(stan="blad" if e.pewny else "czeka", blad=f"Instagram nie przyjął rolki: {e}", zmieniono=teraz())
            zapisz(stan, f"{id_}: błąd kontenera")
            continue
        except Exception as e:  # noqa: BLE001
            s.update(stan="czeka", blad=f"przygotowanie przerwane: {e}"[:300], zmieniono=teraz())
            zapisz(stan, f"{id_}: przerwane")
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
        zapisz(stan, f"{id_}: {s['stan']}")


if __name__ == "__main__":
    if not os.environ.get("KLUCZ"):
        sys.exit("Brak sekretu KLUCZ.")
    obrot()
