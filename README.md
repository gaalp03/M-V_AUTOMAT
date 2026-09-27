# MÁV jegy figyelő

5 percenként megnézi a jegy.mav.hu-n, van-e szabad jegy a heti vonatodra, és
ha van, hangos push értesítést küld a telefonodra ([ntfy](https://ntfy.sh)).

Két vonat van beállítva (`config.json`), mindig a következő alkalomra:

| Név | Mikor | Honnan → hova |
|---|---|---|
| vasárnap | minden vasárnap 15:25 | Szentlőrinc → Budapest-Kelenföld |
| péntek | minden pénteken 16:12 | Budapest-Kelenföld → Szentlőrinc |

## Váltás és kikapcsolás

GitHub (appban vagy böngészőben) → a repó → **Actions** → **MÁV jegy figyelő**
→ **Run workflow** → a **Mit figyeljen?** menüben válaszd ki:

- `vasárnap` – csak a vasárnapi vonatot
- `péntek` – csak a pénteki vonatot
- `mindkettő` – mindkettőt
- `kikapcsolva` – semmit

→ **Run workflow**. Pár másodperc múlva jön egy megerősítő push arról, hogy
mit figyel mostantól.

Egy felszabadult jegyről csak egy értesítés jön. Ha megvetted a jegyet,
kapcsold ki (vagy válts a másik vonatra).

## Beállítás (egyszeri)

1. iPhone: **ntfy** app → **+** → iratkozz fel a topic nevedre.
2. GitHub repó → Settings → Secrets and variables → Actions → Secrets:
   `NTFY_TOPIC` = ugyanez a topic név.

## Egyéb

- **Teszt értesítés:** Run workflow → pipáld be a *Küldjön teszt
  értesítést* opciót. Ez kikapcsolt állapotban is lekérdezi mindkét vonatot,
  és elküldi az aktuális állapotot.
- **Más vonat vagy időpont:** a `config.json`-ban írd át a `honnan`, `hova`,
  `indulas` vagy `nap` mezőt (`hetfo` … `vasarnap`).
- **Osztály:** a `config.json`-ban az `osztaly` értéke lehet `barmelyik`,
  `1` vagy `2`.
- A script a jegy.mav.hu nem hivatalos belső API-ját használja
  (`jegy-a.mav.hu/IK_API_PROD/api`). Ha a MÁV ezt megváltoztatja, a figyelő
  elromolhat; ilyenkor a GitHub Actions futás pirosra vált.
