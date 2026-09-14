# Changelog

## 0.2.1

- Eigenes Icon und Logo (Browserfenster mit Mond: startet bei Bedarf, schlaeft danach) fuer den Addon-Store und die Addon-Seite.
- Favicon fuer die Seite in der Seitenleiste und fuer die Startseite im Browser-Tab.
- "Buy Me A Coffee"-Button in README und Dokumentation, Sponsor-Link auf GitHub.

## 0.2.0

- **Keine Leiste mehr ueber dem Browser.** Das Bild fuellt die ganze Seite. Kacheln, Status, Zwischenablage und Schlafen stehen auf der Startseite, und die zeigt jetzt **jeder neue Tab** (eingebaute Mini-Erweiterung, die die Neuer-Tab-Seite ersetzt). Die doppelte Kachel-Leiste und die Tab-Leiste entfallen - Chromium hat seine eigenen Tabs.
- **Zwischenablage ohne Umweg:** Strg+V fuegt Text vom eigenen Geraet direkt im Browser ein. Im Browser kopierter Text geht direkt in die Zwischenablage des Geraets, wenn der Browser das erlaubt; sonst oeffnet "Zwischenablage" auf der Startseite ein Feld zum Austauschen.
- Die Startseite spricht nur mit einem geheimen Schluessel, der bei jedem Start neu erzeugt wird, mit dem Addon. Andere Webseiten im Browser koennen ihn nicht lesen und damit weder schlafen legen noch die Zwischenablage oeffnen.
- Keine "Diese Seite uebersetzen?"-Blase mehr.
- Aufgeraeumtes Log: kein GPU-Ersatzprozess mehr (spart Speicher), keine Google-Push-Anmeldung, keine D-Bus-/EGL-/GCM-Fehlermeldungen von Chromium.
- Beim Beenden wird das Abmelden uebersprungen, wenn Chromium schon weg ist (vorher "Abmelden unvollstaendig: Connection reset").

## 0.1.0

- Erstes Release: Chromium in der Seitenleiste, der nur bei Nutzung Speicher belegt. Start beim Oeffnen, Einschlafen nach `idle_minutes` ohne Maus-, Tastatur- oder Touch-Eingabe (0 = nie).
- Kacheln fuer eigene Seiten (`sites`) in der Leiste und auf der Startseite, Tab-Liste mit Umschalten und Schliessen, Zwischenablage, Vollbild, Knopf zum sofortigen Schlafenlegen.
- `logout_on_sleep` / `logout_domains`: vor dem Einschlafen werden Tabs der Domain geschlossen, Cookies und Seitendaten geloescht - fuer Online-Banking. WhatsApp und andere Anmeldungen bleiben erhalten.
- Sparsamer Chromium: begrenzte Seitenprozesse, Sparmodus, kein GPU-Prozess, kein Ton; Passwort-Speichern standardmaessig aus.
- Nur ueber Ingress erreichbar; Webseiten im Browser koennen weder Bildstrom noch API noch DevTools erreichen.
