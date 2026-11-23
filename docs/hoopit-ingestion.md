# Hoopit: token og API

Hoopit kobles til med et personlig tilgangstoken og leses gjennom
[Hoopits API](https://api.hoopit.io/external/docs/). Tokenet virker som deg: det
gir tilgang til det du og barna dine kan se i Hoopit-appen, innenfor tilgangene
du krysser av for. Koden står i `hoopit_auth.py` (tokenet), `hoopit.py` (kallene),
`live_hoopit_reader.py` (lister og enkeltelementer) og `live_hoopit.py`
(kommentarer og svar på aktiviteter).

## Slik lager du tokenet

1. Åpne [tokensiden hos Hoopit](https://id.hoopit.io/security/access-tokens/)
   og logg inn som vanlig.
2. Skriv for eksempel «familiekilder» i «Navn».
3. Under «Dine» krysser du av for disse tre:
   - «Se aktivitetene og invitasjonene dine, og barnas»
   - «Svare på invitasjoner for deg selv og barna dine»
   - «Se innlegg og kommentarer i gruppene dine»

   Boksene under «Som administrator» trengs ikke.
4. Velg hvor lenge tokenet skal virke i «Utløper om» (Hoopit foreslår 1 år).
   Når det har utløpt, lager du et nytt på samme måte.
5. Trykk «Opprett et token», og kopier tokenet. Hoopit viser det bare én gang.
   Det begynner med `hoopit_pat_`.
6. Lim det inn i veiviseren (`familiekilder-mcp setup`, «Koble til med token»),
   eller kjør

   ```sh
   familiekilder-mcp configure hoopit
   familiekilder-mcp auth hoopit token
   ```

   og lim inn tokenet når du blir spurt. Det vises ikke mens du limer. Fra en fil
   eller et annet program: `familiekilder-mcp auth hoopit token --stdin < fil`.
   Skriv aldri tokenet som et argument; da havner det i kommandohistorikken.

Programmet kontrollerer først at teksten ser ut som et Hoopit-token. Så leser det
én side innlegg og én side aktiviteter med tokenet. Først når begge lykkes, lagres
tokenet privat i `.private/credentials/hoopit-session.json` (bare lesbart for
deg). Feilmeldingene sier hva som er galt:

| Melding | Hva du gjør |
|---|---|
| Tokenet er ikke helt / ikke et Hoopit-token | Kopier hele tokenet på nytt fra Hoopit |
| Hoopit avviste tokenet. Det er utløpt eller trukket tilbake | Lag et nytt token |
| Tokenet mangler «Se innlegg og kommentarer i gruppene dine» (eller en annen boks) | Lag et nytt token med alle tre boksene under «Dine» avkrysset |

«Svare på invitasjoner for deg selv og barna dine» kan ikke kontrolleres uten å
svare på noe. Mangler den, sier svaret på en aktivitet fra det; lesingen virker
som før.

Hoopits tokenside viser bare boksene, ikke navnene API-et bruker. Sammenhengen,
for den som leser koden eller API-dokumentasjonen:

| Boks under «Dine» | Tilgang i API-et |
|---|---|
| Se aktivitetene og invitasjonene dine, og barnas | `events:read` |
| Svare på invitasjoner for deg selv og barna dine | `events:respond` |
| Se innlegg og kommentarer i gruppene dine | `posts:read` |

Bytt token med `familiekilder-mcp auth hoopit token --replace-existing`, eller
«Bytt token» i veiviseren. `familiekilder-mcp auth hoopit logout` sletter tokenet
her; det virker hos Hoopit til du sletter det på tokensiden.

## Når tokenet slutter å virke

Svarer Hoopit 401 under lesing, merkes tokenet som avvist, og statusen
(`auth hoopit status`, `family_connections`) sier at det er utløpt eller trukket
tilbake og hvordan du lager et nytt. Svarer Hoopit 403 på listen over innlegg
eller aktiviteter, sier statusen hvilken tilgang som mangler. Begge deler står til
du kobler til et nytt token. Programmet prøver ikke på nytt i mellomtiden.

En 403 på kommentarene til ett innlegg gjelder bare det innlegget: innlegget
vises uten kommentarer og med `content_complete=false`, og tokenet beholdes. En
403 på et svar på en aktivitet gir avslag på det svaret (tokenet kan mangle
«Svare på invitasjoner for deg selv og barna dine»), uten å stoppe lesingen.

Kan det lagrede tokenet ikke leses (for eksempel fordi filen er endret for
hånd), sier statusen det. Koble til på nytt og velg å erstatte tokenet, eller
logg ut; begge deler fjerner den uleselige filen.

## Hva som leses

| Kall | Brukes til |
|---|---|
| `GET me/posts/` | Innlegg du eller barna kan lese, nyeste først |
| `GET posts/{id}/comments/` | Kommentarer og svar på ett innlegg, eldste først |
| `GET me/occurrences/?from_date&to_date` | Aktiviteter i et tidsrom, med familiens svar |
| `PATCH events/{id}/occurrences/{id}/responses/{person}/` | Svar på en aktivitet (bare med skriverettigheten `participation`) |

`hoopit_list_items` leser innlegg fra de siste 14 dagene, eller fra `since`.
Innlegg som er opptil 14 dager eldre, får kommentarene sine lest også, fordi en ny
kommentar på et eldre innlegg er nytt innhold. Sidene leses til `has_next=false`,
men stopper etter en side der alle innleggene er eldre enn dette. Aktivitetene
leses fra starten av tidsrommet til 120 dager fram. Hoopit svarer 400 når
`from_date` og `to_date` ligger mer enn 366 dager fra hverandre, så et lengre
tidsrom leses i deler på høyst 365 dager.

Når en grense nås (sider, innlegg, aktiviteter eller forespørsler), gir listen
det som er lest, med `complete=false`, og grensen står i loggen. Lesingen feiler
ikke av den grunn.

`hoopit_get_item` bruker ikke Hoopits kall for ett innlegg eller én aktivitet.
Det finner innlegget i innleggslisten og aktiviteten i aktivitetslisten, så
elementet bygges av de samme feltene som i `hoopit_list_items` og blir likt.
Referansene har med dagen (`day`): opprettelsesdagen for et innlegg og dagen for
en aktivitet. Innleggsoppslaget stopper når listen kommer til innlegg som er
eldre, og aktivitetsoppslaget holder seg til noen få dager. Finnes elementet
ikke, svarer oppslaget at det ikke ble funnet. Hva kallene for ett element gir i
tillegg, står i [det programmet ikke leser ennå](#det-api-et-har-som-programmet-ikke-leser-ennå).

Hoopits innlegg har ingen endringstid. For innlegg er endringstiden den nyeste av
publiseringen og kommentarene. Aktiviteter har `updated_at`, men etter Hoopits
beskrivelse flytter den seg aldri når noen svarer eller kommenterer, og ikke
alltid når aktiviteten endres. Elementet har med familiens svar, så programmet
bruker ikke `updated_at`. Det husker i stedet hvilken versjon av hver aktivitet
det har sett (`hoopit-occurrence-versions.json`, bare ID-er, hasher og
tidspunkt), og `changed_at` er når denne versjonen ble sett første gang. En
aktivitet som ikke er endret, beholder tidspunktet.

Hver kildereferanse har bare ID-er og dagen. Kilde-ID-en er `post:<id>` og
`occurrence:<aktivitet>:<forekomst>`, og lenken går til Hoopit-appen
(`https://app.hoopit.io/app/posts/ID`).

## Versjoner

Hvert kall i Hoopits API har sin egen versjon. Programmet ber om en fast versjon
med headeren `Version` (`ENDPOINTS` i `hoopit.py`; i dag versjon 1 for alle fire).
Svarer Hoopit med `Deprecated-Version` eller `Invalid-Version`, lagres det i
`hoopit-api-version.json`, skrives en advarsel i loggen, og statusen viser
`api_version_warning` til Hoopit slutter å sende headeren. En utfaset versjon
fjernes etter omtrent tre måneder, så oppdater programmet når advarselen kommer.

## Trafikk

Kallene går ett om gangen, med minst ett sekunds mellomrom. Hoopit tillater 5 000
kall i timen per token; ett i sekundet holder seg under det. Svarer Hoopit 429,
lagres `Retry-After`, og lesingen stopper uten nytt forsøk. Nettverksfeil og
serverfeil gir også en lagret pause. Én lesing har høyst 300 kall, 20 sider per
liste og 250 innlegg. Pakkens felles trafikkbudsjett i
[mellomlagring](read-cache.md) gjelder i tillegg.

Gjentatte sider, tomme sider med `has_next=true` og grenser som nås, gir aldri en
melding om full dekning.

## Det Hoopits API ikke har

Per 23. november 2026, etter Hoopits publiserte skjema
(`https://api.hoopit.io/external/docs/schema/`), har API-et ikke dette, så det
er heller ikke med her:

- chatter (Stream), og å sende eller svare i chat
- vedlegg og bilder på innlegg (bilder finnes bare på kommentarer)
- varsler
- mottakerne av et innlegg, og kontaktgrupper for vanlige medlemmer
- betaling for aktiviteter
- å skrive kommentarer på aktiviteter
- når et innlegg sist ble endret

## Det API-et har som programmet ikke leser ennå

Skjemaet har også disse kallene for vanlige medlemmer. Programmet bruker dem
ikke ennå. Tilgangene de krever, dekkes av de tre boksene under «Dine» som du
krysser av for, så tokenet trenger ingen nye bokser for å lese dem.

| Kall | Tilgang | Gir |
|---|---|---|
| `GET me/` | alle tokens | Hvem tokenet virker som: ID og fullt navn |
| `GET me/children/` | `events:read`, `posts:read` eller `events:respond` | Barna du styrer: ID og fullt navn |
| `GET me/events/{id}/occurrences/{id}/` | `events:read` | Én aktivitet med feltene fra listen og i tillegg beskrivelse (`description`) og svarfrist (`response_deadline`) |
| `GET me/events/{id}/occurrences/{id}/comments/` | `events:read` | Kommentarer og svar på én aktivitet, eldste først |
| `GET posts/{id}/` | `posts:read` | Ett innlegg, med de samme feltene som i listen |
| `GET me/post-comments/?since=` | `posts:read` | Kommentarer og svar laget fra et tidspunkt, på innleggene du eller barna kan lese, med innleggets ID |

En manuell prøve 23. november 2026 bekreftet at de to kallene for én
aktivitetsforekomst returnerte beskrivelse og kommentarer. Feltet
`response_deadline` var tomt i forekomstene som ble prøvd. En utfylt svarfrist
og de øvrige kallene i tabellen gjenstår å prøve.

Kommentarer på innlegg og aktiviteter har `images` (ID og adresse per bilde).
Programmet tar bare med teksten. `me/occurrences/`, `me/posts/` og
`me/post-comments/` finnes også under `users/{person}/`, for deg eller ett av
barna.

Skjemaet har dessuten `POST posts/{id}/comments/`, som skriver en kommentar
eller et svar på en kommentar som deg, med tilgangen `posts:comment`. Programmet
tilbyr bare lesing og ber ikke om den tilgangen. Kallene som krever boksene
under «Som administrator» (`club.events:read` og `club.members:read`), virker
bare for administratorer i klubber som har slått på API-tilgang, og brukes ikke
her.

## Kontobinding

Programmet spør ikke Hoopit hvem tokenet tilhører. Det gir tokenøkten en
tilfeldig kontobinding første gang du kobler til. Mellomlagrede oppslag hører
til denne bindingen. Bytter du token, beholdes den; logger du ut, starter neste
token en ny. Vil du bytte til en annen Hoopit-konto, logg ut først.

API-et har `GET me/` (se over), men programmet bruker det ikke til bindingen. Å
binde til Hoopit-kontoen i stedet ville endre bindingen for tokenøkter som
finnes, og mellomlagrede oppslag fra før ville ikke bli brukt igjen.

## Prøv tokenet uten å lagre det

`scripts/hoopit_smoke.py` leser med et token mot Hoopit uten å bruke eller endre
datamappen din. Det gjør bare GET-kall og skriver ut antall, ID-er, titler og
tidspunkt, aldri tokenet, tekster eller navn:

```sh
python -I scripts/hoopit_smoke.py --token-file sti/til/tokenfil --posts 5 --days 14
```

Kjør det fra `familiekilder`-mappen med et Python-miljø der pakken er installert.
Tokenet kan også sendes på stdin.

Svar på aktiviteter står i [funksjonsmatrisen](service-capabilities.md#hoopit).
