# MÁV jegy figyelő

Figyeli egy adott MÁV vonat jegyértékesítését, és amint felszabadul rajta egy
jegy, azonnal küld egy nagyon látszó (sürgős, hangos) push értesítést a
telefonodra az [ntfy.sh](https://ntfy.sh) szolgáltatáson keresztül.

Alapértelmezett beállítás: **Szentlőrinc → Budapest-Kelenföld, Mecsek
InterCity, 15:25-ös indulás, a mai napon.**

A script a jegy.mav.hu weboldal nem hivatalos, nem dokumentált belső API-ját
hívja (`jegy-a.mav.hu/IK_API_PROD/api`), közösségi reverse-engineering
projektek alapján.

## 1. ntfy topic beállítása

1. Telepítsd a **ntfy** appot ([Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy),
   [iOS](https://apps.apple.com/us/app/ntfy/id1625396347)), vagy nyisd meg
   https://ntfy.sh a böngészőben.
2. Válassz egy egyedi, nehezen kitalálható "topic" nevet (ez olyan, mint egy
   csatornanév, pl. `sztl-bp-jegy-x7k2`), és iratkozz fel rá az appban.
   Bárki, aki ismeri a topic nevet, tud rá üzenetet küldeni, ezért ne legyen
   túl egyszerű.

## 2. GitHub repo beállítása

A repo **Settings → Secrets and variables → Actions** alatt:

- **Secrets** fül → új secret: `NTFY_TOPIC` = a fent kiválasztott topic név.
- **Variables** fül (opcionális, ha mást szeretnél figyelni, mint az
  alapértelmezett):
  - `FROM_STATION` (alapérték: `Szentlőrinc`)
  - `TO_STATION` (alapérték: `Budapest-Kelenföld`)
  - `TRAVEL_DATE` (alapérték: a mai nap, formátum `YYYY-MM-DD`)
  - `TRAIN_TIME` (alapérték: `15:25`)
  - `TRAIN_NAME_HINT` (alapérték: `Mecsek`)
  - `WANTED_CLASS` (alapérték: `any` – bármelyik osztály; lehet `1` vagy `2` is)

Utána a **Settings → Actions → General** alatt engedélyezd az Actions
futását, ha még nincs bekapcsolva.

A workflow (`.github/workflows/watch-ticket.yml`) 5 percenként lefut, amíg a
megadott vonat indulási időpontja el nem múlik – utána a script automatikusan
nem csinál semmit (nem hív API-t feleslegesen).

Egy felszabadult jegyről csak egyszer jön értesítés; ha a vonat újra betelik,
majd megint felszabadul hely, akkor jön újabb.

## 3. Hogyan dönti el, hogy van-e jegy?

A MÁV ajánlatkérő válaszában a vonat `travelClasses` listája csak azokat az
osztályokat tartalmazza, amelyekre még lehet jegyet venni (pl. ha a 2. osztály
betelt, csak `"1"` szerepel benne). A script akkor riaszt, ha a `WANTED_CLASS`
osztály megjelenik ebben a listában, és a vásárlás nincs letiltva.

Minden futás logjában látszik az aktuális állapot, pl.:

```
Szentlőrinc -> Budapest-Kelenföld (15:25): nincs jegy - elérhető osztályok: ['1'], szabad hely: Keves
```

Kézi futtatásnál (`Run workflow`) a **debug** opcióval az összes aznapi
vonat összefoglalója is kiíródik.

## 4. Ha megvetted a jegyet / elmúlt a vasárnap

Kapcsold ki a workflow-t: **Actions → MÁV jegy figyelő → ⋯ → Disable
workflow**, különben minden érintett napon 5 percenként tovább fut.

## Helyi futtatás (teszteléshez)

```bash
pip install -r requirements.txt
export NTFY_TOPIC=sztl-bp-jegy-x7k2
export DEBUG=1
python mav_ticket_watch.py
```
