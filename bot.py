import os
import logging
import asyncio
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

import mlb_api
import bullpen
import storage

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
POLL_MINUTES = float(os.getenv("POLL_MINUTES", "3"))
ROSTER_REFRESH_HOURS = float(os.getenv("ROSTER_REFRESH_HOURS", "6"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bullpen_bot")

intents = discord.Intents.default()

# In-memory cache of bullpens already computed today, so the end-of-day edge
# alert doesn't have to recompute all 30 teams from scratch.
_bullpen_cache: dict[str, dict[int, list[dict]]] = {}  # {report_date: {team_id: bullpen}}


def et_date_str(offset_days: int = 0) -> str:
    et = datetime.now(timezone.utc) - timedelta(hours=4)
    et += timedelta(days=offset_days)
    return et.strftime("%Y-%m-%d")


def get_today_schedule(date_str: str) -> list[dict]:
    import requests
    resp = requests.get(
        "https://statsapi.mlb.com/api/v1/schedule",
        params={"sportId": 1, "date": date_str},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    games = []
    for date_entry in data.get("dates", []):
        for g in date_entry.get("games", []):
            games.append({
                "game_pk": g["gamePk"],
                "abstract_state": g["status"].get("abstractGameState"),
                "home_team_id": g["teams"]["home"]["team"]["id"],
                "away_team_id": g["teams"]["away"]["team"]["id"],
            })
    return games


def build_report_embed(team: dict, bullpen_list: list[dict], notes: list[str], report_date: str) -> discord.Embed:
    groups = {"red": [], "yellow": [], "green": []}
    for p in bullpen_list:
        groups[p["status"]].append(p["name"])

    lines = []
    if groups["green"]:
        lines.append(f"🟢 {', '.join(groups['green'])}")
    if groups["yellow"]:
        lines.append(f"🟡 {', '.join(groups['yellow'])}")
    if groups["red"]:
        lines.append(f"🔴 {', '.join(groups['red'])}")

    embed = discord.Embed(
        title=f"{team['name']} Bullpen Report",
        description="\n".join(lines) if lines else "No bullpen data available.",
        color=discord.Color.blue(),
    )
    if notes:
        embed.add_field(name="Heads up", value="\n".join(notes), inline=False)
    embed.set_footer(text=f"{report_date} • Data: MLB Stats API")
    return embed


def build_edge_embed(edge_notes: list[str], report_date: str) -> discord.Embed:
    embed = discord.Embed(
        title="⚠️ Edge Alert",
        description="\n".join(edge_notes) if edge_notes else "No platoon edges today.",
        color=discord.Color.red(),
    )
    embed.set_footer(text=f"{report_date} • Data: MLB Stats API")
    return embed


def build_lastapp_embed(pitcher_name: str, splits: list[dict]) -> discord.Embed:
    if not splits:
        return discord.Embed(title=pitcher_name, description="No game log found for this season yet.")
    last = splits[-1]
    embed = discord.Embed(
        title=f"{pitcher_name} — Last Appearance",
        description=(
            f"{last['date']} vs {last['opponent']}\n\n"
            f"**{last['pitches']} pitches** • {last['ip']} IP\n"
            f"{last['hits']}H {last['er']}ER {last['bb']}BB {last['so']}K"
        ),
        color=discord.Color.blue(),
    )
    if len(splits) >= 2:
        recent = splits[-5:][::-1]
        lines = [f"{s['date']}: {s['pitches']}p" for s in recent]
        embed.add_field(name="Recent outings", value="\n".join(lines), inline=False)
    embed.set_footer(text="Data: MLB Stats API")
    return embed


class BullpenBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.teams: list[dict] = []
        self.teams_by_id: dict[int, dict] = {}
        self.player_directory: list[dict] = []  # [{"id":, "name":, "team":}]

    async def setup_hook(self):
        storage.init_db()

        try:
            self.teams = mlb_api.get_all_teams()
        except Exception as e:
            log.error("Failed to fetch team list at startup: %s", e)
            self.teams = []
        self.teams_by_id = {t["id"]: t for t in self.teams}

        await self.refresh_player_directory()

        for team in self.teams:
            self._register_team_command(team)

        lastapp_cmd = app_commands.Command(
            name="lastapp",
            description="Show a reliever's most recent appearance",
            callback=self._lastapp_callback,
        )
        self.tree.add_command(lastapp_cmd)
        lastapp_cmd.autocomplete("name")(self._name_autocomplete)

        markreliever_cmd = app_commands.Command(
            name="markreliever",
            description="Override: always treat this pitcher as a reliever (e.g. bullpen-game opener)",
            callback=self._markreliever_callback,
        )
        self.tree.add_command(markreliever_cmd)
        markreliever_cmd.autocomplete("name")(self._name_autocomplete)

        unmarkreliever_cmd = app_commands.Command(
            name="unmarkreliever",
            description="Remove a previous /markreliever override",
            callback=self._unmarkreliever_callback,
        )
        self.tree.add_command(unmarkreliever_cmd)
        unmarkreliever_cmd.autocomplete("name")(self._name_autocomplete)

        setchannel_cmd = app_commands.Command(
            name="setchannel",
            description="Set this channel to receive bullpen reports and edge alerts",
            callback=self._setchannel_callback,
        )
        self.tree.add_command(setchannel_cmd)

        edge_cmd = app_commands.Command(
            name="edge",
            description="Check platoon edges (0 LHP/RHP available) across MLB right now",
            callback=self._edge_callback,
        )
        self.tree.add_command(edge_cmd)

        allbullpens_cmd = app_commands.Command(
            name="allbullpens",
            description="Post every team's bullpen report right now (useful for testing)",
            callback=self._allbullpens_callback,
        )
        self.tree.add_command(allbullpens_cmd)

        try:
            synced = await self.tree.sync()
            log.info("Synced %d slash commands", len(synced))
        except Exception as e:
            log.error("Slash command sync failed: %s", e)

    async def refresh_player_directory(self):
        directory = []
        for team in self.teams:
            try:
                pitchers = await asyncio.to_thread(mlb_api.get_active_roster, team["id"])
            except Exception as e:
                log.error("Failed to fetch roster for team %s: %s", team["id"], e)
                continue
            for p in pitchers:
                directory.append({"id": p["id"], "name": p["name"], "team": team["abbreviation"]})
        self.player_directory = directory
        log.info("Player directory refreshed: %d pitchers", len(directory))

    def _register_team_command(self, team: dict):
        cmd_name = f"{team['abbreviation'].lower()}bullpen"
        callback = self._make_team_callback(team)

        command = app_commands.Command(
            name=cmd_name,
            description=f"{team['name']} bullpen availability right now",
            callback=callback,
        )
        self.tree.add_command(command)

    def _make_team_callback(self, team: dict):
        async def callback(interaction: discord.Interaction):
            await interaction.response.defer()
            report_date = et_date_str(0)
            try:
                bp, notes = await asyncio.to_thread(bullpen.build_team_bullpen, team, report_date)
            except Exception as e:
                await interaction.followup.send(f"Couldn't build bullpen report right now: {e}")
                return
            await interaction.followup.send(embed=build_report_embed(team, bp, notes, report_date))
        return callback

    async def _name_autocomplete(self, interaction: discord.Interaction, current: str):
        current_lower = current.lower()
        matches = [p for p in self.player_directory if current_lower in p["name"].lower()][:25]
        return [
            app_commands.Choice(name=f"{p['name']} ({p['team']})", value=str(p["id"]))
            for p in matches
        ]

    def _resolve_pitcher(self, name: str):
        if name.isdigit():
            pid = int(name)
            match = next((p for p in self.player_directory if p["id"] == pid), None)
            return (pid, match["name"]) if match else (pid, name)
        match = next((p for p in self.player_directory if name.lower() in p["name"].lower()), None)
        return (match["id"], match["name"]) if match else (None, name)

    async def _lastapp_callback(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer()
        person_id, pitcher_name = self._resolve_pitcher(name)
        if person_id is None:
            await interaction.followup.send(f"Couldn't find a pitcher matching '{name}'.")
            return
        try:
            splits = mlb_api.get_pitcher_game_log(person_id)
        except Exception as e:
            await interaction.followup.send(f"Couldn't reach the MLB API right now: {e}")
            return
        await interaction.followup.send(embed=build_lastapp_embed(pitcher_name, splits))

    async def _markreliever_callback(self, interaction: discord.Interaction, name: str):
        person_id, pitcher_name = self._resolve_pitcher(name)
        if person_id is None:
            await interaction.response.send_message(f"Couldn't find a pitcher matching '{name}'.")
            return
        storage.add_reliever_override(person_id, pitcher_name)
        await interaction.response.send_message(
            f"✅ {pitcher_name} will now always be treated as a reliever in bullpen reports."
        )

    async def _unmarkreliever_callback(self, interaction: discord.Interaction, name: str):
        person_id, pitcher_name = self._resolve_pitcher(name)
        if person_id is None:
            await interaction.response.send_message(f"Couldn't find a pitcher matching '{name}'.")
            return
        storage.remove_reliever_override(person_id)
        await interaction.response.send_message(f"✅ Removed reliever override for {pitcher_name}.")

    async def _setchannel_callback(self, interaction: discord.Interaction):
        storage.set_config("announce_channel_id", str(interaction.channel_id))
        await interaction.response.send_message(
            f"✅ Bullpen reports and edge alerts will post in {interaction.channel.mention}."
        )

    async def _edge_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        report_date = et_date_str(0)
        cached = _bullpen_cache.get(report_date, {})

        edge_notes = []
        for team in self.teams:
            bp = cached.get(team["id"])
            if bp is None:
                try:
                    bp, _ = await asyncio.to_thread(bullpen.build_team_bullpen, team, report_date)
                    _bullpen_cache.setdefault(report_date, {})[team["id"]] = bp
                except Exception as e:
                    log.error("Edge check failed for team %s: %s", team["id"], e)
                    continue
            edge_notes.extend(bullpen.find_edges(team["abbreviation"], bp))

        await interaction.followup.send(embed=build_edge_embed(edge_notes, report_date))

    async def _allbullpens_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        report_date = et_date_str(0)
        await interaction.followup.send(f"Building all 30 bullpen reports for {report_date}, posting as they're ready...")

        for team in sorted(self.teams, key=lambda t: t["name"]):
            try:
                bp, notes = await asyncio.to_thread(bullpen.build_team_bullpen, team, report_date)
                _bullpen_cache.setdefault(report_date, {})[team["id"]] = bp
            except Exception as e:
                log.error("Failed to build bullpen for %s: %s", team["abbreviation"], e)
                continue
            try:
                await interaction.channel.send(embed=build_report_embed(team, bp, notes, report_date))
            except Exception as e:
                log.error("Failed to send bullpen report for %s: %s", team["abbreviation"], e)

    async def on_ready(self):
        log.info("Logged in as %s", self.user)
        if not poll_bullpens.is_running():
            poll_bullpens.start(self)
        if not refresh_directory_loop.is_running():
            refresh_directory_loop.start(self)


client = BullpenBot()


@tasks.loop(minutes=POLL_MINUTES)
async def poll_bullpens(bot: BullpenBot):
    channel_id = storage.get_config("announce_channel_id")
    if not channel_id:
        return
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        return

    report_date = et_date_str(0)
    try:
        games = await asyncio.to_thread(get_today_schedule, report_date)
    except Exception as e:
        log.error("Failed to fetch today's schedule: %s", e)
        return

    if not games:
        return

    _bullpen_cache.setdefault(report_date, {})

    teams_played_today = set()
    for g in games:
        teams_played_today.add(g["home_team_id"])
        teams_played_today.add(g["away_team_id"])

    # Gap-fill FIRST, using coverage as it stood before this cycle touches
    # anything -- a team scheduled today whose last covered date is stale
    # (e.g. an off day yesterday broke the normal game-end trigger chain)
    # gets a fresh push instead of staying silent until their game ends.
    today_str = et_date_str(0)
    for team_id in teams_played_today:
        last_covered = storage.get_last_check_date(team_id)
        if last_covered and last_covered >= today_str:
            continue  # already covered today (or even further ahead) -- not stale
        team = bot.teams_by_id.get(team_id)
        if not team:
            continue
        try:
            bp, notes = await asyncio.to_thread(bullpen.build_team_bullpen, team, today_str)  # check_date defaults to today_str
            if last_covered:  # None means "never posted for this team before" -- not an off-day gap
                notes = ["⏸️ Team had an off day, refreshing availability for today."] + notes
        except Exception as e:
            log.error("Gap-fill failed for team %s: %s", team_id, e)
            continue
        storage.set_last_check_date(team_id, today_str)
        storage.mark_report_sent(team_id, today_str)
        try:
            await channel.send(embed=build_report_embed(team, bp, notes, today_str))
            log.info("Posted gap-fill bullpen report for %s", team["abbreviation"])
        except Exception as e:
            log.error("Failed to send gap-fill report for team %s: %s", team_id, e)

    for g in games:
        if g["abstract_state"] != "Final":
            continue
        for team_id in (g["home_team_id"], g["away_team_id"]):
            if storage.report_already_sent(team_id, report_date):
                continue
            team = bot.teams_by_id.get(team_id)
            if not team:
                continue
            try:
                bp, notes = await asyncio.to_thread(bullpen.build_team_bullpen, team, report_date)
            except Exception as e:
                log.error("Failed to build bullpen for team %s: %s", team_id, e)
                continue

            _bullpen_cache[report_date][team_id] = bp
            storage.mark_report_sent(team_id, report_date)
            storage.set_last_check_date(team_id, report_date)
            try:
                await channel.send(embed=build_report_embed(team, bp, notes, report_date))
                log.info("Posted bullpen report for %s", team["abbreviation"])
            except Exception as e:
                log.error("Failed to send bullpen report for team %s: %s", team_id, e)

    all_final = all(g["abstract_state"] == "Final" for g in games)
    if all_final and not storage.edge_alert_already_sent(report_date):
        edge_notes = []
        for team_id in teams_played_today:
            team = bot.teams_by_id.get(team_id)
            if not team:
                continue
            bp = _bullpen_cache.get(report_date, {}).get(team_id)
            if bp is None:
                try:
                    bp, _ = await asyncio.to_thread(bullpen.build_team_bullpen, team, report_date)
                except Exception as e:
                    log.error("Failed to build bullpen for edge alert, team %s: %s", team_id, e)
                    continue
            edge_notes.extend(bullpen.find_edges(team["abbreviation"], bp))

        storage.mark_edge_alert_sent(report_date)
        if edge_notes:
            try:
                await channel.send(embed=build_edge_embed(edge_notes, report_date))
                log.info("Posted edge alert with %d notes", len(edge_notes))
            except Exception as e:
                log.error("Failed to send edge alert: %s", e)


@poll_bullpens.before_loop
async def before_poll():
    await client.wait_until_ready()


@tasks.loop(hours=ROSTER_REFRESH_HOURS)
async def refresh_directory_loop(bot: BullpenBot):
    await bot.refresh_player_directory()


@refresh_directory_loop.before_loop
async def before_refresh():
    await client.wait_until_ready()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your .env file (see .env.example).")
    client.run(TOKEN)
