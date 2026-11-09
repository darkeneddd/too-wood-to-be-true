"""Thin wrapper around the Catanatron engine.

Adds what the engine lacks for LLM play:
  * fixed seat order (engine shuffles seats by default)
  * player-to-player ("domestic") trades
  * player-chosen discards on a 7 (engine discards randomly by default)
  * the real rule that a dev card can't be played on the turn it was bought
  * JSON snapshots of the full (omniscient) state for the viewer
"""

from collections import Counter

from catanatron import Game, Color
from catanatron.models.player import Player
from catanatron.models.enums import (
    RESOURCES,
    DEVELOPMENT_CARDS,
    Action,
    ActionType,
    ActionPrompt,
)
from catanatron.models.actions import generate_playable_actions
from catanatron.state_functions import (
    player_key,
    player_has_rolled,
    player_num_resource_cards,
    player_deck_subtract,
    player_freqdeck_add,
    player_resource_freqdeck_contains,
)

DEV_PLAY_ACTIONS = {
    ActionType.PLAY_KNIGHT_CARD: "KNIGHT",
    ActionType.PLAY_YEAR_OF_PLENTY: "YEAR_OF_PLENTY",
    ActionType.PLAY_MONOPOLY: "MONOPOLY",
    ActionType.PLAY_ROAD_BUILDING: "ROAD_BUILDING",
}


class Seat(Player):
    """Placeholder player; decisions are made by our runner, not by decide()."""

    def decide(self, game, playable_actions):  # pragma: no cover
        raise RuntimeError("Seat.decide should never be called")


def freq_from_dict(d):
    """{'WOOD': 1, ...} (any case) -> [wood, brick, sheep, wheat, ore]"""
    norm = {k.upper(): int(v) for k, v in d.items()}
    return [max(0, norm.get(r, 0)) for r in RESOURCES]


def freq_to_text(freq):
    parts = [f"{n} {r}" for r, n in zip(RESOURCES, freq) if n]
    return ", ".join(parts) if parts else "nothing"


class GameSession:
    def __init__(self, colors_in_order, seed, vps_to_win=10):
        players = [Seat(c) for c in colors_in_order]
        self.game = Game(players, seed=seed, vps_to_win=vps_to_win)
        self._force_seat_order(players)
        self.bought_this_turn = {c: Counter() for c in colors_in_order}

    # ------------------------------------------------------------ setup
    def _force_seat_order(self, players):
        st = self.game.state
        st.players = list(players)
        st.colors = tuple(p.color for p in players)
        st.color_to_index = {c: i for i, c in enumerate(st.colors)}
        st.current_player_index = 0
        st.current_turn_index = 0
        st.playable_actions = generate_playable_actions(st)

    # ------------------------------------------------------------ queries
    @property
    def state(self):
        return self.game.state

    @property
    def board_map(self):
        return self.game.state.board.map

    def current_color(self):
        return self.state.current_color()

    def turn_color(self):
        return self.state.colors[self.state.current_turn_index]

    def winner(self):
        return self.game.winning_color()

    def ps(self, color, field):
        return self.state.player_state[f"{player_key(self.state, color)}_{field}"]

    def hand(self, color):
        return [self.ps(color, f"{r}_IN_HAND") for r in RESOURCES]

    def dev_hand(self, color):
        return {d: self.ps(color, f"{d}_IN_HAND") for d in DEVELOPMENT_CARDS}

    def num_cards(self, color):
        return player_num_resource_cards(self.state, color)

    def is_discarding(self):
        return self.state.current_prompt == ActionPrompt.DISCARD

    def can_domestic_trade(self):
        st = self.state
        color = st.current_color()
        return (
            not st.is_initial_build_phase
            and st.current_prompt == ActionPrompt.PLAY_TURN
            and not st.is_road_building
            and st.current_player_index == st.current_turn_index
            and player_has_rolled(st, color)
            and self.num_cards(color) > 0
        )

    def legal_actions(self):
        """Engine actions minus dev cards bought this very turn."""
        color = self.current_color()
        out = []
        for a in self.state.playable_actions:
            card = DEV_PLAY_ACTIONS.get(a.action_type)
            if card is not None:
                playable = self.ps(color, f"{card}_IN_HAND") - self.bought_this_turn[color][card]
                if playable < 1:
                    continue
            out.append(a)
        return out

    # ------------------------------------------------------------ mutations
    def execute(self, action):
        result = self.game.execute(action)
        if result.action_type == ActionType.BUY_DEVELOPMENT_CARD:
            self.bought_this_turn[result.color][result.value] += 1
        elif result.action_type == ActionType.END_TURN:
            self.bought_this_turn[result.color].clear()
        return result

    def discard(self, color, freq):
        """Discard a specific set of cards. freq = [wood, brick, sheep, wheat, ore].
        Raises ValueError if invalid."""
        if self.current_color() != color or not self.is_discarding():
            raise ValueError("not this player's discard")
        need = self.num_cards(color) // 2
        hand = self.hand(color)
        if sum(freq) != need:
            raise ValueError(f"must discard exactly {need} cards, got {sum(freq)}")
        for r, n, h in zip(RESOURCES, freq, hand):
            if n > h:
                raise ValueError(f"cannot discard {n} {r}, only have {h}")
        listdeck = [r for r, n in zip(RESOURCES, freq) for _ in range(n)]
        return self.game.execute(Action(color, ActionType.DISCARD, listdeck), validate_action=False)

    def random_discard(self, color):
        return self.game.execute(Action(color, ActionType.DISCARD, None), validate_action=False)

    def check_trade(self, giver, gives, receiver, receives):
        """giver hands `gives` to receiver and gets `receives` back. Returns error str or None."""
        if sum(gives) == 0 or sum(receives) == 0:
            return "both sides of a trade must include at least one card (no gifts)"
        for r, a, b in zip(RESOURCES, gives, receives):
            if a and b:
                return f"{r} appears on both sides of the trade"
        if not player_resource_freqdeck_contains(self.state, giver, gives):
            return f"{giver.value} does not have {freq_to_text(gives)}"
        if not player_resource_freqdeck_contains(self.state, receiver, receives):
            return f"{receiver.value} does not have {freq_to_text(receives)}"
        return None

    def domestic_trade(self, a, a_gives, b, b_gives):
        err = self.check_trade(a, a_gives, b, b_gives)
        if err:
            raise ValueError(err)
        st = self.state
        player_deck_subtract(st, a, a_gives)
        player_freqdeck_add(st, b, a_gives)
        player_deck_subtract(st, b, b_gives)
        player_freqdeck_add(st, a, b_gives)
        st.playable_actions = generate_playable_actions(st)

    # ------------------------------------------------------------ snapshot
    def snapshot(self):
        st = self.state
        b = st.board
        roads = {}
        for edge, color in b.roads.items():
            roads[tuple(sorted(edge))] = color.value
        players = {}
        for c in st.colors:
            players[c.value] = {
                "vp": self.ps(c, "VICTORY_POINTS"),
                "vp_actual": self.ps(c, "ACTUAL_VICTORY_POINTS"),
                "hand": dict(zip(RESOURCES, self.hand(c))),
                "dev": self.dev_hand(c),
                "knights_played": self.ps(c, "PLAYED_KNIGHT"),
                "longest_road": self.ps(c, "LONGEST_ROAD_LENGTH"),
                "has_road": self.ps(c, "HAS_ROAD"),
                "has_army": self.ps(c, "HAS_ARMY"),
                "settlements_left": self.ps(c, "SETTLEMENTS_AVAILABLE"),
                "cities_left": self.ps(c, "CITIES_AVAILABLE"),
                "roads_left": self.ps(c, "ROADS_AVAILABLE"),
            }
        return {
            "buildings": [[n, c.value, t] for n, (c, t) in b.buildings.items()],
            "roads": [[e[0], e[1], col] for e, col in roads.items()],
            "robber_tile": b.map.land_tiles[b.robber_coordinate].id,
            "players": players,
            "bank": dict(zip(RESOURCES, st.resource_freqdeck)),
            "dev_deck": len(st.development_listdeck),
            "num_turns": st.num_turns,
            "current": st.current_color().value,
            "turn_player": self.turn_color().value,
        }


ALL_COLORS = [Color.RED, Color.BLUE, Color.ORANGE, Color.WHITE]
