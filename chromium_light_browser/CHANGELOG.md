# Changelog

## 0.1.0

- Erstes Release: Chromium in der Seitenleiste, der nur bei Nutzung Speicher belegt. Start beim Oeffnen, Einschlafen nach `idle_minutes` ohne Maus-, Tastatur- oder Touch-Eingabe (0 = nie).
- Kacheln fuer eigene Seiten (`sites`) in der Leiste und auf der Startseite, Tab-Liste mit Umschalten und Schliessen, Zwischenablage, Vollbild, Knopf zum sofortigen Schlafenlegen.
- `logout_on_sleep` / `logout_domains`: vor dem Einschlafen werden Tabs der Domain geschlossen, Cookies und Seitendaten geloescht - fuer Online-Banking. WhatsApp und andere Anmeldungen bleiben erhalten.
- Sparsamer Chromium: begrenzte Seitenprozesse, Sparmodus, kein GPU-Prozess, kein Ton; Passwort-Speichern standardmaessig aus.
- Nur ueber Ingress erreichbar; Webseiten im Browser koennen weder Bildstrom noch API noch DevTools erreichen.
