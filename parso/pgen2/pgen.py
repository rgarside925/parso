# Copyright 2004-2005 Elemental Security, Inc. All Rights Reserved.
# Licensed to PSF under a Contributor Agreement.

# Modifications:
# Copyright David Halter and Contributors
# Modifications are dual-licensed: MIT and PSF.

"""
Specifying grammars in pgen is possible with this grammar::

    grammar: (NEWLINE | rule)* ENDMARKER
    rule: NAME ':' rhs NEWLINE
    rhs: items ('|' items)*
    items: item+
    item: '[' rhs ']' | atom ['+' | '*']
    atom: '(' rhs ')' | NAME | STRING

This grammar is self-referencing.
"""

from parso.pgen2.grammar import Grammar
from parso.python import token
from parso.pgen2.grammar_parser import GrammarParser, NFAState


class ParserGenerator(object):
    def __init__(self, rule_to_dfas, token_namespace):
        self._token_namespace = token_namespace
        self._nonterminal_to_dfas = rule_to_dfas

    def make_grammar(self, grammar):
        self._first = {}  # map from symbol name to set of tokens

        names = list(self._nonterminal_to_dfas.keys())
        names.sort()
        for name in names:
            if name not in self._first:
                self._calcfirst(name)
            #print name, self._first[name].keys()

            i = 256 + len(grammar.symbol2number)
            grammar.symbol2number[name] = i
            grammar.number2symbol[i] = name

        for name in names:
            dfas = self._nonterminal_to_dfas[name]
            states = []
            for state in dfas:
                arcs = []
                for label, next in state.arcs.items():
                    arcs.append((self._make_label(grammar, label), dfas.index(next)))
                if state.isfinal:
                    arcs.append((0, dfas.index(state)))
                states.append(arcs)
            grammar.states.append(states)
            grammar.dfas[grammar.symbol2number[name]] = (states, self._make_first(grammar, name))
        return grammar

    def _make_first(self, grammar, name):
        rawfirst = self._first[name]
        first = set()
        for label in rawfirst:
            ilabel = self._make_label(grammar, label)
            ##assert ilabel not in first, "%s failed on <> ... !=" % label
            first.add(ilabel)
        return first

    def _make_label(self, grammar, label):
        # XXX Maybe this should be a method on a subclass of converter?
        ilabel = len(grammar.labels)
        if label[0].isalpha():
            # Either a symbol name or a named token
            if label in grammar.symbol2number:
                # A symbol name (a non-terminal)
                if label in grammar.symbol2label:
                    return grammar.symbol2label[label]
                else:
                    grammar.labels.append((grammar.symbol2number[label], None))
                    grammar.symbol2label[label] = ilabel
                    grammar.label2symbol[ilabel] = label
                    return ilabel
            else:
                # A named token (NAME, NUMBER, STRING)
                itoken = getattr(self._token_namespace, label, None)
                assert isinstance(itoken, int), label
                if itoken in grammar.tokens:
                    return grammar.tokens[itoken]
                else:
                    grammar.labels.append((itoken, None))
                    grammar.tokens[itoken] = ilabel
                    return ilabel
        else:
            # Either a keyword or an operator
            assert label[0] in ('"', "'"), label
            value = eval(label)
            if value[0].isalpha():
                # A keyword
                if value in grammar.keywords:
                    return grammar.keywords[value]
                else:
                    grammar.labels.append((token.NAME, value))
                    grammar.keywords[value] = ilabel
                    return ilabel
            else:
                # An operator (any non-numeric token)
                itoken = self._token_namespace.generate_token_id(value)
                if itoken in grammar.tokens:
                    return grammar.tokens[itoken]
                else:
                    grammar.labels.append((itoken, None))
                    grammar.tokens[itoken] = ilabel
                    return ilabel

    def _calcfirst(self, name):
        dfas = self._nonterminal_to_dfas[name]
        self._first[name] = None  # dummy to detect left recursion
        state = dfas[0]
        totalset = set()
        overlapcheck = {}
        for nonterminal_or_string, next in state.arcs.items():
            if nonterminal_or_string in self._nonterminal_to_dfas:
                # It's a nonterminal and we have either a left recursion issue
                # in the grammare or we have to recurse.
                try:
                    fset = self._first[nonterminal_or_string]
                except KeyError:
                    self._calcfirst(nonterminal_or_string)
                    fset = self._first[nonterminal_or_string]
                else:
                    if fset is None:
                        raise ValueError("left recursion for rule %r" % name)
                totalset.update(fset)
                overlapcheck[nonterminal_or_string] = fset
            else:
                # It's a string. We have finally found a possible first token.
                totalset.add(nonterminal_or_string)
                overlapcheck[nonterminal_or_string] = set([nonterminal_or_string])
        inverse = {}
        for nonterminal_or_string, itsfirst in overlapcheck.items():
            for symbol in itsfirst:
                if symbol in inverse:
                    raise ValueError("rule %s is ambiguous; %s is in the"
                                     " first sets of %s as well as %s" %
                                     (name, symbol, nonterminal_or_string, inverse[symbol]))
                inverse[symbol] = nonterminal_or_string
        self._first[name] = totalset


class DFAState(object):
    def __init__(self, from_rule, nfa_set, final):
        assert isinstance(nfa_set, set)
        assert isinstance(next(iter(nfa_set)), NFAState)
        assert isinstance(final, NFAState)
        self.from_rule = from_rule
        self.nfa_set = nfa_set
        self.isfinal = final in nfa_set
        self.arcs = {}  # map from nonterminals or strings to DFAState

    def add_arc(self, next_, label):
        assert isinstance(label, str)
        assert label not in self.arcs
        assert isinstance(next_, DFAState)
        self.arcs[label] = next_

    def unifystate(self, old, new):
        for label, next in self.arcs.items():
            if next is old:
                self.arcs[label] = new

    def __eq__(self, other):
        # Equality test -- ignore the nfa_set instance variable
        assert isinstance(other, DFAState)
        if self.isfinal != other.isfinal:
            return False
        # Can't just return self.arcs == other.arcs, because that
        # would invoke this method recursively, with cycles...
        if len(self.arcs) != len(other.arcs):
            return False
        for label, next in self.arcs.items():
            if next is not other.arcs.get(label):
                return False
        return True

    __hash__ = None  # For Py3 compatibility.


def _simplify_dfas(dfas):
    # This is not theoretically optimal, but works well enough.
    # Algorithm: repeatedly look for two states that have the same
    # set of arcs (same labels pointing to the same nodes) and
    # unify them, until things stop changing.

    # dfas is a list of DFAState instances
    changes = True
    while changes:
        changes = False
        for i, state_i in enumerate(dfas):
            for j in range(i + 1, len(dfas)):
                state_j = dfas[j]
                if state_i == state_j:
                    #print "  unify", i, j
                    del dfas[j]
                    for state in dfas:
                        state.unifystate(state_j, state_i)
                    changes = True
                    break


def _make_dfas(start, finish):
    """
    This is basically doing what the powerset construction algorithm is doing.
    """
    # To turn an NFA into a DFA, we define the states of the DFA
    # to correspond to *sets* of states of the NFA.  Then do some
    # state reduction.
    assert isinstance(start, NFAState)
    assert isinstance(finish, NFAState)

    def addclosure(nfa_state, base_nfa_set):
        assert isinstance(nfa_state, NFAState)
        if nfa_state in base_nfa_set:
            return
        base_nfa_set.add(nfa_state)
        for nfa_arc in nfa_state.arcs:
            if nfa_arc.nonterminal_or_string is None:
                addclosure(nfa_arc.next, base_nfa_set)

    base_nfa_set = set()
    addclosure(start, base_nfa_set)
    states = [DFAState(start.from_rule, base_nfa_set, finish)]
    for state in states:  # NB states grows while we're iterating
        arcs = {}
        # Find state transitions and store them in arcs.
        for nfa_state in state.nfa_set:
            for nfa_arc in nfa_state.arcs:
                if nfa_arc.nonterminal_or_string is not None:
                    nfa_set = arcs.setdefault(nfa_arc.nonterminal_or_string, set())
                    addclosure(nfa_arc.next, nfa_set)

        # Now create the dfa's with no None's in arcs anymore. All Nones have
        # been eliminated and state transitions (arcs) are properly defined, we
        # just need to create the dfa's.
        for nonterminal_or_string, nfa_set in arcs.items():
            for nested_state in states:
                if nested_state.nfa_set == nfa_set:
                    # The DFA state already exists for this rule.
                    break
            else:
                nested_state = DFAState(start.from_rule, nfa_set, finish)
                states.append(nested_state)

            state.add_arc(nested_state, nonterminal_or_string)
    return states  # List of DFAState instances; first one is start


def _dump_nfa(start, finish):
    print("Dump of NFA for", start.from_rule)
    todo = [start]
    for i, state in enumerate(todo):
        print("  State", i, state is finish and "(final)" or "")
        for label, next in state.arcs:
            if next in todo:
                j = todo.index(next)
            else:
                j = len(todo)
                todo.append(next)
            if label is None:
                print("    -> %d" % j)
            else:
                print("    %s -> %d" % (label, j))


def _dump_dfas(dfas):
    print("Dump of DFA for", dfas[0].from_rule)
    for i, state in enumerate(dfas):
        print("  State", i, state.isfinal and "(final)" or "")
        for nonterminal, next in state.arcs.items():
            print("    %s -> %d" % (nonterminal, dfas.index(next)))


def generate_grammar(bnf_grammar, token_namespace):
    """
    ``bnf_text`` is a grammar in extended BNF (using * for repetition, + for
    at-least-once repetition, [] for optional parts, | for alternatives and ()
    for grouping).

    It's not EBNF according to ISO/IEC 14977. It's a dialect Python uses in its
    own parser.
    """
    rule_to_dfas = {}
    start_symbol = None
    for nfa_a, nfa_z in GrammarParser(bnf_grammar).parse():
        #_dump_nfa(a, z)
        dfas = _make_dfas(nfa_a, nfa_z)
        #_dump_dfas(dfas)
        # oldlen = len(dfas)
        _simplify_dfas(dfas)
        # newlen = len(dfas)
        rule_to_dfas[nfa_a.from_rule] = dfas
        #print(nfa_a.from_rule, oldlen, newlen)

        if start_symbol is None:
            start_symbol = nfa_a.from_rule

    p = ParserGenerator(rule_to_dfas, token_namespace)
    return p.make_grammar(Grammar(bnf_grammar, start_symbol))
