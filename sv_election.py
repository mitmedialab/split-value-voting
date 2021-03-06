# sv_election.py
# python3

""" Defines election class, and can run simulated election.
"""

# MIT open-source license.
# (See https://github.com/ron-rivest/split-value-voting.git)

import sv
import sv_prover
import sv_server
import sv_race
import sv_sbb
import sv_tally
import sv_voter

class Election:
    """ Implements a (simulated) election. """

    def __init__(self, election_parameters):
        """ Initialize election object.

        Initialize election, where election_parameters is a dict
        with at least the following key/values:
            "election_id" is a string
            "ballot_style" is a list of (race_id, choices) pairs,
               in which
                   race_id is a string
                   choices is list consisting of
                       one string for each allowable candidate/choice name, or
                       a string "******************" of stars
                           of the maximum allowable length of a write-in
                           if write-ins are allowed.

                Example:
                  ballot_style = [("President", ("Smith", "Jones", "********"))]
                defines a ballot style with one race (for President), and
                for this race the voter may vote for Smith, for Jones, or
                may cast a write-in vote of length at most 8 characters.
            "n_voters" is the number of simulated voters
            "n_reps" is the parameter for the cut-and-choose step
                     (n_reps replicas are made)
                     (in our paper, n_reps is called "2m")
            "n_fail" is the number of servers that may fail
            "n_leak" is the number of servers that may leak
        """

        self.election_parameters = election_parameters

        # manadatory parameters
        election_id = election_parameters["election_id"]
        ballot_style = election_parameters["ballot_style"]
        n_voters = election_parameters["n_voters"]
        n_reps = election_parameters["n_reps"]
        n_fail = election_parameters["n_fail"]
        n_leak = election_parameters["n_leak"]
        # optional parameters (with defaults)
        ballot_id_len = election_parameters.get("ballot_id_len", 32)
        json_indent = election_parameters.get("json_indent", 0)
        self.json_indent = json_indent
        sv.set_json_indent(json_indent)

        # check and save parameters
        assert isinstance(election_id, str) and len(election_id) > 0
        self.election_id = election_id

        assert isinstance(ballot_style, list) and len(ballot_style) > 0
        self.ballot_style = ballot_style

        assert isinstance(n_voters, int) and n_voters > 0
        self.n_voters = n_voters
        # p-list is list ["p0", "p1", ..., "p(n-1)"]
        # these name the positions in a list of objects, one per voter.
        # they are not voter ids, they are really names of positions
        # used for clarity and greater compatibility with json
        # keep all the same length (use leading zeros) so that
        # json "sort by keys" options works.
        # the following list is in sorted order!
        self.p_list = sv.p_list(n_voters)

        assert isinstance(n_reps, int) and \
            0 < n_reps <= 26 and n_reps % 2 == 0
        self.n_reps = n_reps
        # Each copy will be identified by an upper-case ascii letter
        self.k_list = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"[:n_reps]

        assert isinstance(n_fail, int) and n_fail >= 0
        assert isinstance(n_leak, int) and n_leak >= 0
        self.n_fail = n_fail
        self.n_leak = n_leak

        assert ballot_id_len > 0
        self.ballot_id_len = ballot_id_len

        about_text = \
        ["Secure Bulletin Board for Split-Value Voting Method Demo.",
         "by Michael O. Rabin and Ronald L. Rivest",
         "For paper: see http://people.csail.mit.edu/rivest/pubs.html#RR14a",
         "For code: see https://github.com/ron-rivest/split-value-voting",
        ]
        legend_text = \
        ["Indices between 0 and n_voters-1 indicated by p0, p1, ...",
         "Rows of server array indicated by a, b, c, d, ...",
         "Copies (n_reps = 2m passes) indicated by A, B, C, D, ...",
         "'********' in ballot style indicates a write-in option",
         "           (number of stars is max write-in length)",
         "Values represented are represented modulo race_modulus.",
         "'x' (or 'y') equals u+v (mod race_modulus),",
         "             and is a (Shamir-)share of the vote.",
         "'cu' and 'cv' are commitments to u and v, respectively.",
         "'ru' and 'rv' are randomization values for cu and cv.",
         "'icl' stands for 'input comparison list',",
         "'opl' for 'output production list';",
         "      these are the 'cut-and-choose' results",
         "      dividing up the lists into two sub-lists.",
         "'time' is time in ISO 8601 format."
        ]
        # start secure bulletin board
        self.sbb = sv_sbb.SBB(election_id)
        self.sbb.post("setup:start",
                      {"about": about_text,
                       "election_id": election_id,
                       "legend": legend_text
                      }
                     )

        self.races = []
        self.race_ids = [race_id for (race_id, choices) in ballot_style]
        self.setup_races(ballot_style)
        self.voters = []
        self.voter_ids = []
        self.setup_voters(self, n_voters)
        self.cast_votes = dict()
        self.server = sv_server.Server(self, n_fail, n_leak)
        self.output_commitments = dict()
        self.setup_keys()
        self.sbb.post("setup:finished")

    def run_election(self):
        """ Run a (simulated) election. """

        self.initialize_cast_votes()

        # Vote !
        for voter in self.voters:
            for race in self.races:
                voter.cast_vote(race)

        # send votes to mix-net
        self.distribute_cast_votes()

        # post vote commitments on SBB
        self.post_cast_vote_commitments()

        # post voter receipts on SBB
        self.post_voter_receipts()

        # Mix !
        self.server.mix()

        # Tally!
        sv_tally.compute_tally(self)
        sv_tally.post_tally(self)
        sv_tally.print_tally(self)

        # Prove!
        sv_prover.make_proof(self)

        # Stop election and close sbb
        self.sbb.post("election:done.",
                      {"election_id": self.election_id})
        self.sbb.close()

    def setup_races(self, ballot_style):
        """ Set up races for this election, where ballot_style is
        a list of (race_id, choices) pairs, and where
            race_id is a string
            choices is list consisting of
               one string for each allowable candidate/choice name
               a string "******************" of stars
                  of the maximum allowable length of a write-in
                  if write-ins are allowed.

        Example:
          ballot_style = [("President", ("Smith", "Jones", "********"))]
             defines a ballot style with one race (for President), and
             for this race the voter may vote for Smith, for Jones, or
             may cast a write-in vote of length at most 8 characters.
        """
        # check that race_id's are distinct:
        race_ids = [race_id for (race_id, choices) in ballot_style]
        assert len(race_ids) == len(set(race_ids))

        race_dict = dict()
        for (race_id, choices) in ballot_style:
            race = sv_race.Race(self, race_id, choices)
            self.races.append(race)
            race_dict[race_id] = {"choices": race.choices,
                                  "race_modulus": race.race_modulus}
        self.sbb.post("setup:races",
                      {"ballot_style_race_dict": race_dict},
                      time_stamp=False)

    def setup_voters(self, election, n_voters):
        """ Set up election to have n_voters voters in this simulation. """

        assert isinstance(n_voters, int) and n_voters > 0

        # voter identifier is merely "voter:" + index: voter:0, voter:1, ...
        for i in range(n_voters):
            vid = "voter:" + str(i)
            self.voter_ids.append(vid)
            px = election.p_list[i]
            voter = sv_voter.Voter(self, vid, px)
            self.voters.append(voter)

        self.sbb.post("setup:voters",
                      {"n_voters": n_voters,
                       'ballot_id_len': election.ballot_id_len},
                      time_stamp=False)

        if False:
            if n_voters <= 3:
                self.sbb.post("(setup:voter_ids)",
                              {"list": self.voter_ids})
            else:
                self.sbb.post("(setup:voter_ids)",
                              {"list": (self.voter_ids[0],
                                        "...",
                                        self.voter_ids[-1])},
                              time_stamp=False)

    def initialize_cast_votes(self):
        """ Initialize the election data structure to receive the cast votes.

            This data structure is updated by voter.cast_vote in sv_voter.py
        """
        cvs = dict()
        for race_id in self.race_ids:
            cvs[race_id] = dict()
            for px in self.p_list:
                cvs[race_id][px] = dict()    # maps row i to vote share
        self.cast_votes = cvs

    def setup_keys(self):
        """ Set up cryptographic keys for this election simulation.

        Not done here in this simulation for simplicity.
        """
        pass

    def distribute_cast_votes(self):
        """ Distribute cast votes to server data structure. """
        for race_id in self.race_ids:
            for px in self.p_list:
                for i in self.server.row_list:
                    vote = self.cast_votes[race_id][px][i]
                    # save these values in our server data structures
                    # in a non-simulated real election, this would be done
                    # by communicating securely from voter (or tablet) to the
                    # first column of servers.
                    sdbp = self.server.sdb[race_id][i][0]
                    sdbp['ballot_id'][px] = vote['ballot_id']
                    sdbp['x'][px] = vote['x']
                    sdbp['u'][px] = vote['u']
                    sdbp['v'][px] = vote['v']
                    sdbp['ru'][px] = vote['ru']
                    sdbp['rv'][px] = vote['rv']
                    sdbp['cu'][px] = vote['cu']
                    sdbp['cv'][px] = vote['cv']

    def post_cast_vote_commitments(self):
        """ Post cast vote commitments onto SBB. """
        cvs = self.cast_votes
        cvcs = dict()
        for race_id in self.race_ids:
            cvcs[race_id] = dict()
            for px in self.p_list:
                cvcs[race_id][px] = dict()
                for i in self.server.row_list:
                    cvcs[race_id][px][i] = dict()
                    cvcs[race_id][px][i]['ballot_id'] = \
                        cvs[race_id][px][i]['ballot_id']
                    cvcs[race_id][px][i]['cu'] = \
                        cvs[race_id][px][i]['cu']
                    cvcs[race_id][px][i]['cv'] = \
                        cvs[race_id][px][i]['cv']
        self.sbb.post("casting:votes",
                      {"cast_vote_dict": cvcs},
                      time_stamp=False)

    def post_voter_receipts(self):
        """ Post all voter receipts on the SBB. """
        receipts = dict()
        for voter in self.voters:
            for ballot_id in voter.receipts:
                receipts[ballot_id] = voter.receipts[ballot_id]
        self.sbb.post("casting:receipts",
                      {"receipt_dict": receipts},
                      time_stamp=False)


