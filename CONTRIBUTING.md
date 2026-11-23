# Bidra til familiekilder-mcp

Takk for at du vil hjelpe til. Du kan bidra uten å skrive kode: si fra når en
kobling har sluttet å virke, be om en ny tjeneste eller del en prompt som
virker.

Alle som deltar, følger [retningslinjene for oppførsel](CODE_OF_CONDUCT.md).

## Det du deler, kan alle lese

Saker, diskusjoner og pull requests er offentlige. Del aldri innlogging,
passord, tokens, øktfiler, callback-adresser, navn på barn eller innhold fra
ekte beskjeder. Står noe slikt i en feilmelding, bytt det ut med `…`.

## Meld fra om en kobling som ikke virker

Opprett en sak under **Issues** og velg **En tjeneste virker ikke**. Skjemaet
spør etter versjonen (`familiekilder-mcp --version`) og rapporten fra
`familiekilder-mcp doctor`. Rapporten utelater stien til dataroten,
profilnavn og innhold fra tjenestene, men les den før du deler den.

Tjenestene kan endre appene sine uten å si fra. Da kan en kobling slutte å
virke for alle samtidig, så se først om noen alt har meldt fra.

Si også gjerne fra når noe virker, særlig på Windows og Linux; se
[Prøve på Windows eller Linux](docs/prove-pa-windows-og-linux.md).

## Be om en ny tjeneste

Opprett en sak under **Issues** og velg **Ønsk en ny tjeneste**. Fortell hva
tjenesten heter, hva dere bruker den til, og om den har app, nettside eller
begge deler.

## Spørsmål og prompter

Still spørsmål under **Discussions**, i kategorien **Q&A**. Del prompter som
virker, i kategorien **Show and tell**.

## Sikkerhetsfeil

Meld aldri en sikkerhetsfeil i en offentlig sak. Følg
[sikkerhetsrutinen](SECURITY.md).

## Sende en pull request

1. Lag en fork og en egen gren for endringen.
2. Gjør endringen, med tester for det du endrer.
3. Kjør testene og kontrollen av koden:

   ```sh
   scripts/bootstrap-local-venv
   scripts/test                          # alle testene, hver fil i egen prosess
   scripts/test tests/test_spond.py      # bare én fil
   uvx ruff@0.17.4 check .
   ```

   Testene bruker bare oppdiktede data, falske transporter og midlertidige
   mapper, og kontakter ingen ekte tjeneste.

4. Beskriv i pull requesten hva du har endret, og hvorfor.

`scripts/bootstrap-local-venv` og `scripts/test` er skrevet for macOS og
Linux. På Windows kan du kjøre hele kontrollen med `python scripts/check-release`.
Den trenger [uv](https://docs.astral.sh/uv/) og er den samme kontrollen som
kjører på GitHub, på Linux og Windows, for hver pull request.

Vil du koble til en ny tjeneste, start med
[veiledningen for nye tjenester](docs/adding-a-service.md). Hvorfor ting er
bygget som de er, står i [arkitekturen](docs/arkitektur.md).

### Slik tas en pull request inn

Vedlikeholderne bruker ikke knappen for å flette på GitHub. De tar inn
endringen med et eget verktøy, og den kommer med i repoet ved neste
oppdatering. Da vises pull requesten som flettet (**Merged**), og commitene
dine blir med i historikken. Til det skjer, står pull requesten åpen, også
etter at den er godkjent.

## Reglene du må kjenne

- **Serveren skal bare lese.** Kommandolinjen og MCP-serveren skal ikke kunne
  sende, svare, melde på, slette eller merke noe som lest hos en tjeneste.
  Se [hva tjenestene støtter](docs/service-capabilities.md).
- **Bare oppdiktede data.** Ekte svar fra en tjeneste, navn og ID-er skal ikke
  inn i Git, heller ikke som fixture. Lag syntetiske svar med samme form.
- **Ingen hemmeligheter.** Innlogging, tokens, økter og private nøkler skal
  aldri i Git, i saker eller i logger.
- **Tjenestelisten kommer fra `sources.REGISTRY`.** En ny tjeneste registreres
  i `src/familiekilder/sources.py`. Lister over tjenester lages derfra, ikke
  for hånd. Der en liste står i tekst, som i README, kontrollerer en test at den
  stemmer.
- **Testene kontakter ingen ekte tjeneste.** Bruk falske transporter fra
  `tests/support/familiekilder_testing/`.
- **Språk.** Kode og kommentarer skrives på engelsk. Dokumentasjonen skrives
  på norsk bokmål i klarspråk.

Alle reglene for kode og tilgang står i [AGENTS.md](AGENTS.md).
