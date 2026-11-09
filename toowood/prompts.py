"""Everything the agents read: rules briefing, state view, move labels, reply schemas."""

from catanatron.models.enums import ActionType, RESOURCES

from .engine import freq_to_text

# ---------------------------------------------------------------- system prompt

RULES = """You are a player in a 4-player game of Catan (standard base-game rules). The other three players are also AI agents.

GOAL: be the first to reach 10 victory points (VP) on your own turn.

VP SOURCES: settlement 1, city 2, Longest Road (5+ connected roads, longest of all) 2, Largest Army (3+ knights played, most of all) 2, Victory Point dev card 1 (hidden until the game ends).

COSTS:
  road = 1 WOOD + 1 BRICK
  settlement = 1 WOOD + 1 BRICK + 1 SHEEP + 1 WHEAT  (must be on your road network, and no settlement may be adjacent to another)
  city = 2 WHEAT + 3 ORE  (upgrades one of your settlements; produces double)
  development card = 1 SHEEP + 1 WHEAT + 1 ORE

DICE & PRODUCTION: each turn starts with a roll of two dice. Every tile with that number produces its resource for each adjacent settlement (1) or city (2). "Pips" measure how often a number rolls (6 and 8 = 5 pips, the best; 2 and 12 = 1 pip). The tile with the robber produces nothing.

ROLLING A 7: everyone with more than 7 resource cards discards half (rounded down). The roller then moves the robber to another tile and steals 1 random card from a player with a building on it.

DEVELOPMENT CARDS: Knight (move the robber and steal), Road Building (2 free roads), Year of Plenty (take 2 resources from the bank), Monopoly (all players give you every card of one named resource), Victory Point. You may play at most one development card per turn, and never on the same turn you bought it. A Knight may be played before rolling.

SETUP: each player places a settlement and a road, then in reverse order a second settlement and road. The second settlement immediately produces one of each adjacent resource.

TRADING:
  * Bank: 4 identical cards for 1 of your choice; 3:1 with a generic port; 2:1 with that resource's port. You must have a settlement or city on the port.
  * Players: on your own turn, after rolling, you may propose trades to the other players. Any player may accept, reject or counter. You then choose which (if any) to take. Gifts (cards for nothing) are not allowed. Trades always happen between the player whose turn it is and one other player.

TABLE TALK: anything you put in "say" is spoken aloud to all players at the table. Like at a real table, you can say what you like, and others may or may not be telling the truth. Leave "say" empty to stay quiet.

YOUR NOTEBOOK: you have a private notebook that only you can read. It is shown to you every time you decide, across the whole game. Use "notebook" to rewrite it (e.g. your plan, what you believe about opponents), or leave it empty to keep it unchanged.

REASON: "reason" is a brief explanation of your decision. It is recorded in the game log for later review and is never shown to other players.

BOARD REFERENCE: tiles are T<id>, intersections (where settlements go) are N<id>, roads are written N<a>-N<b>. The fixed board layout is below; the current buildings, robber and hands are given with every decision."""


def system_prompt(board_text):
    return RULES + "\n\n=== FIXED BOARD LAYOUT ===\n" + board_text


# ---------------------------------------------------------------- reply schemas

def _res_obj(desc):
    return {
        "type": "object",
        "description": desc,
        "properties": {r.lower(): {"type": "integer"} for r in RESOURCES},
        "required": [r.lower() for r in RESOURCES],
        "additionalProperties": False,
    }


ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "reason": {"type": "string", "description": "Brief explanation of your decision (log only)."},
        "choice": {"type": "integer", "description": "Index of the chosen option."},
        "say": {"type": "string", "description": "Public table talk, or empty string."},
        "notebook": {"type": "string", "description": "Full new notebook text, or empty string to keep it."},
        "trade_give": _res_obj("Only when proposing a trade: cards you would give. Otherwise all zeros."),
        "trade_get": _res_obj("Only when proposing a trade: cards you want in return. Otherwise all zeros."),
    },
    "required": ["reason", "choice", "say", "notebook", "trade_give", "trade_get"],
    "additionalProperties": False,
}

RESPOND_SCHEMA = {
    "type": "object",
    "properties": {
        "reason": {"type": "string", "description": "Brief explanation of your decision (log only)."},
        "response": {"type": "string", "enum": ["accept", "reject", "counter"]},
        "counter_you_give": _res_obj("Only for a counter: cards YOU would give. Otherwise all zeros."),
        "counter_you_get": _res_obj("Only for a counter: cards YOU would receive. Otherwise all zeros."),
        "say": {"type": "string", "description": "Public table talk, or empty string."},
        "notebook": {"type": "string", "description": "Full new notebook text, or empty string to keep it."},
    },
    "required": ["reason", "response", "counter_you_give", "counter_you_get", "say", "notebook"],
    "additionalProperties": False,
}

DISCARD_SCHEMA = {
    "type": "object",
    "properties": {
        "reason": {"type": "string", "description": "Brief explanation of your decision (log only)."},
        "discard": _res_obj("Exactly the cards to discard."),
        "say": {"type": "string", "description": "Public table talk, or empty string."},
        "notebook": {"type": "string", "description": "Full new notebook text, or empty string to keep it."},
    },
    "required": ["reason", "discard", "say", "notebook"],
    "additionalProperties": False,
}

# ---------------------------------------------------------------- move labels


def describe_action(a, session, board):
    t = a.action_type
    v = a.value
    if t == ActionType.ROLL:
        return "Roll the dice"
    if t == ActionType.END_TURN:
        return "End your turn"
    if t == ActionType.BUILD_SETTLEMENT:
        return f"Build a settlement at {board.node_summary(v)}"
    if t == ActionType.BUILD_CITY:
        return f"Upgrade your settlement at {board.node_summary(v)} to a city"
    if t == ActionType.BUILD_ROAD:
        a_, b_ = v
        return f"Build a road N{a_}-N{b_}"
    if t == ActionType.BUY_DEVELOPMENT_CARD:
        return "Buy a development card"
    if t == ActionType.PLAY_KNIGHT_CARD:
        return "Play a Knight card (move the robber and steal)"
    if t == ActionType.PLAY_ROAD_BUILDING:
        return "Play Road Building (build 2 free roads)"
    if t == ActionType.PLAY_MONOPOLY:
        return f"Play Monopoly on {v}"
    if t == ActionType.PLAY_YEAR_OF_PLENTY:
        return f"Play Year of Plenty: take {' + '.join(v)}"
    if t == ActionType.MARITIME_TRADE:
        give = [r for r in v[:-1] if r is not None]
        return f"Bank trade: give {len(give)} {give[0]}, get 1 {v[-1]}"
    if t == ActionType.MOVE_ROBBER:
        coord, victim, _ = v
        tid = board.coord_to_tile[coord]
        t_ = board.tiles[tid]
        owners = sorted({c.value for n in t_["nodes"]
                         for (c, _k) in [session.state.board.buildings.get(n, (None, None))] if c})
        where = f"Move the robber to {board.tile_label(tid)} ({t_['pips']} pips; buildings: {', '.join(owners) or 'none'})"
        if victim is None:
            return where + ", steal from nobody"
        return where + f", steal from {victim.value} ({session.num_cards(victim)} cards)"
    if t == ActionType.DISCARD:
        return "Discard"
    return f"{t.value} {v}"


# ---------------------------------------------------------------- state view


def state_view(session, board, color, notebook, events, chat, game_round):
    st = session.state
    lines = []
    lines.append(f"You are {color.value}. Round {game_round}; it is {session.turn_color().value}'s turn.")
    lines.append(f"Seat order: {', '.join(c.value for c in st.colors)}.")
    lines.append("")
    lines.append("SCOREBOARD (public):")
    for c in st.colors:
        badges = []
        if session.ps(c, "HAS_ROAD"):
            badges.append("LONGEST ROAD")
        if session.ps(c, "HAS_ARMY"):
            badges.append("LARGEST ARMY")
        dev_count = sum(session.dev_hand(c).values())
        me = " (you)" if c == color else ""
        lines.append(
            f"  {c.value}{me}: {session.ps(c, 'VICTORY_POINTS')} VP visible, "
            f"{session.num_cards(c)} resource cards, {dev_count} dev cards in hand, "
            f"{session.ps(c, 'PLAYED_KNIGHT')} knights played, road length {session.ps(c, 'LONGEST_ROAD_LENGTH')}"
            + (f" [{', '.join(badges)}]" if badges else "")
        )
    lines.append("")
    hand = session.hand(color)
    lines.append(f"YOUR HAND: {freq_to_text(hand)}  ({sum(hand)} cards)")
    dev = {k: n for k, n in session.dev_hand(color).items() if n}
    if dev:
        new = session.bought_this_turn[color]
        dev_txt = ", ".join(
            f"{n} {k}" + (f" ({new[k]} bought this turn, not playable yet)" if new[k] else "")
            for k, n in dev.items()
        )
    else:
        dev_txt = "none"
    lines.append(f"YOUR DEV CARDS: {dev_txt}")
    lines.append(f"YOUR TRUE VP (incl. hidden VP cards): {session.ps(color, 'ACTUAL_VICTORY_POINTS')}")
    ports = sorted({board.node_port[n] for n, (c, _k) in st.board.buildings.items() if c == color and n in board.node_port})
    lines.append(f"YOUR PORTS: {', '.join(ports) if ports else 'none'}")
    lines.append(
        f"BANK: {', '.join(f'{n} {r}' for r, n in zip(RESOURCES, st.resource_freqdeck))}; "
        f"{len(st.development_listdeck)} dev cards left"
    )
    lines.append("")
    robber_tid = board.coord_to_tile[st.board.robber_coordinate]
    lines.append(f"ROBBER: on {board.tile_label(robber_tid)}")
    lines.append("BUILDINGS & ROADS:")
    for c in st.colors:
        setts = sorted(n for n, (cc, k) in st.board.buildings.items() if cc == c and k == "SETTLEMENT")
        cities = sorted(n for n, (cc, k) in st.board.buildings.items() if cc == c and k == "CITY")
        roads = sorted({tuple(sorted(e)) for e, cc in st.board.roads.items() if cc == c})
        lines.append(f"  {c.value}:")
        lines.append(f"    settlements: {', '.join(board.node_summary(n) for n in setts) or 'none'}")
        lines.append(f"    cities: {', '.join(board.node_summary(n) for n in cities) or 'none'}")
        lines.append(f"    roads: {', '.join(f'N{a}-N{b}' for a, b in roads) or 'none'}")
    lines.append("")
    lines.append("RECENT EVENTS (oldest first):")
    lines.extend(f"  {e}" for e in events) if events else lines.append("  (none yet)")
    lines.append("")
    lines.append("RECENT TABLE TALK (oldest first):")
    lines.extend(f"  {m}" for m in chat) if chat else lines.append("  (nobody has spoken yet)")
    lines.append("")
    lines.append("YOUR NOTEBOOK:")
    lines.append(notebook.strip() if notebook.strip() else "  (empty)")
    return "\n".join(lines)


def options_block(labels):
    return "OPTIONS:\n" + "\n".join(f"  {i}: {lab}" for i, lab in enumerate(labels))
