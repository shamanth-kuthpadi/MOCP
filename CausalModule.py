# causal-learn imports
from causallearn.search.ConstraintBased.PC import pc
from causallearn.search.ScoreBased.GES import ges
from causallearn.search.FCMBased import lingam
from causallearn.utils.PDAG2DAG import pdag2dag
from causallearn.search.FCMBased.lingam.utils import make_dot

# dowhy imports
import dowhy.gcm.falsify
from dowhy.gcm.falsify import falsify_graph
from dowhy.gcm.falsify import apply_suggestions
from dowhy import CausalModel

# utility imports
from utilities.utils import *

# https://stackoverflow.com/questions/79673823/dowhy-python-library-module-networkx-algorithms-has-no-attribute-d-separated
import networkx as nx
nx.algorithms.d_separated = nx.algorithms.d_separation.is_d_separator
nx.d_separated = nx.algorithms.d_separation.is_d_separator

import logging

logging.basicConfig(
    filename="pipeline_debug_output.txt",
    filemode="w",  # Overwrite each run; use "a" to append
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s: %(message)s"
)


class CausalModule:
    def __init__(self, 
                 data = None, 
                 discovery_algorithm = None, 
                 treatment_variable = None, 
                 outcome_variable = None,
                 treatment_value = None,
                 control_value = None):
        
        # user input
        self.data = data
        self.discovery_algorithm = discovery_algorithm
        self.treatment_variable = treatment_variable
        self.outcome_variable = outcome_variable
        self.treatment_value = treatment_value
        self.control_value = control_value
        
        
        self.graph = None
        self.graph_ref = None
        self.model = None
        self.estimand = None
        self.estimate = None
        self.est_ref = None
    
    # For now, the only prior knowledge that the prototype will allow is required/forbidden edges
    # pk must be of the type => {'required': [list of edges to require], 'forbidden': [list of edges to forbid]}
    def find_causal_graph(self, algo='pc', pk=None):
        
        if self.discovery_algorithm:
            algo = self.discovery_algorithm
        
        logging.info(f"Finding causal graph using {algo} algorithm")
        
        df = self.data.to_numpy()
        labels = list(self.data.columns)
        
        try:
            match algo:
                case 'pc':
                    cg = pc(data=df, show_progress=True, node_names=labels, verbose=False)
                    cg = pdag2dag(cg.G)
                    predicted_graph = genG_to_nx(cg, labels)
                    self.graph = predicted_graph
                case 'ges':
                    cg = ges(X=df, node_names=labels)
                    cg = pdag2dag(cg['G'])
                    predicted_graph = genG_to_nx(cg, labels)
                    self.graph = predicted_graph
                case 'icalingam':
                    model = lingam.ICALiNGAM()
                    model.fit(df)
                    pyd_lingam = make_dot(model.adjacency_matrix_, labels=labels)
                    pyd_lingam = pyd_lingam.pipe(format='dot').decode('utf-8')
                    pyd_lingam = (pyd_lingam,) = graph_from_dot_data(pyd_lingam)
                    dot_data_lingam = pyd_lingam.to_string()
                    pydot_graph_lingam = graph_from_dot_data(dot_data_lingam)[0]
                    predicted_graph = nx.drawing.nx_pydot.from_pydot(pydot_graph_lingam)
                    predicted_graph = nx.DiGraph(predicted_graph)
                    self.graph = predicted_graph
            
            if pk is not None:
                # ensuring that pk is indeed of the right type
                if not isinstance(pk, dict):
                    logging.info(f"Please ensure that the prior knowledge is of the right form")
                    raise
                # are there any edges to require
                if 'required' in pk.keys():
                    eb = pk['required']
                    self.graph.add_edges_from(eb)
                # are there any edges to remove
                if 'forbidden' in pk.keys():
                    eb = pk['forbidden']
                    self.graph.remove_edges_from(eb)
        
        except Exception as e:
            logging.error(f"Error in creating causal graph: {e}")
            raise

        return self.graph

    # What if user already has a graph they would like to input
    def input_causal_graph(self, graph):
        self.graph = graph

    def refute_cgm(self, n_perm=100, indep_test=gcm, cond_indep_test=gcm, apply_sugst=True, show_plt=False):
        
        logging.info("Refuting the discovered/given causal graph")
        
        try:
            result = falsify_graph(self.graph, self.data, n_permutations=n_perm,
                                  independence_test=indep_test,
                                  conditional_independence_test=cond_indep_test, plot_histogram=show_plt)
            
            self.graph_ref = result
            
            if apply_sugst:
                self.graph = apply_suggestions(self.graph, result)
            
        except Exception as e:
            logging.error(f"Error in refuting graph: {e}")
            raise

        return self.graph
    
    def create_model(self):
        
        logging.info("Creating a causal model from the discovered/given causal graph")
        
        model_est = CausalModel(
                data=self.data,
                treatment=self.treatment_variable,
                outcome=self.outcome_variable,
                graph=self.graph
            )
        self.model = model_est
        return self.model

    def identify_effect(self, method=None):
        
        logging.info("Identifying the effect estimand of the treatment on the outcome variable")
        
        try:
            if method is None:
                identified_estimand = self.model.identify_effect()
            else:
                identified_estimand = self.model.identify_effect(method=method)

            self.estimand = identified_estimand

            # Add logging if estimand is None or not identified
            if self.estimand is None or not hasattr(self.estimand, 'estimand_type'):
                logging.warning("Warning: Could not identify a valid estimand from the discovered causal graph. Please check the graph structure or variable selection.")
        except Exception as e:
            logging.error(f"Error in identifying effect: {e}")
            raise

        logging.info("Note that you can also use other methods for the identification process. Below are method descriptions taken directly from DoWhy's documentation")
        logging.info("maximal-adjustment: returns the maximal set that satisfies the backdoor criterion. This is usually the fastest way to find a valid backdoor set, but the set may contain many superfluous variables.")
        logging.info("minimal-adjustment: returns the set with minimal number of variables that satisfies the backdoor criterion. This may take longer to execute, and sometimes may not return any backdoor set within the maximum number of iterations.")
        logging.info("exhaustive-search: returns all valid backdoor sets. This can take a while to run for large graphs.")
        logging.info("default: This is a good mix of minimal and maximal adjustment. It starts with maximal adjustment which is usually fast. It then runs minimal adjustment and returns the set having the smallest number of variables.")
        return self.estimand
    
    def estimate_effect(self, method_cat='backdoor.linear_regression', ctrl_val=None, trtm_val=None):
        
        logging.info("Estimating the effect of the treatment on the outcome variable")
        
        if ctrl_val is None:
            ctrl_val = self.control_value
        if trtm_val is None:
            trtm_val = self.treatment_value
        
        estimate = None
        try:
            match method_cat:
                case 'backdoor.linear_regression':
                    estimate = self.model.estimate_effect(self.estimand,
                                                  method_name=method_cat,
                                                  control_value=ctrl_val,
                                                  treatment_value=trtm_val,
                                                  confidence_intervals=True,
                                                  test_significance=True)
                # there are other estimation methods that I can add later on, however parameter space will increase immensely
            self.estimate = estimate
        except Exception as e:
            logging.error(f"Error in estimating the effect: {e}")
            raise
        
        logging.info("Note that it is ok for your treatment to be a continuous variable, DoWhy automatically discretizes at the backend.")
        return self.estimate
    
    # should give a warning to users if the estimate is to be refuted

    def refute_estimate(self,  method_name="ALL", placebo_type='permute', subset_fraction=0.9):
        
        logging.info("Refuting the estimated effect of the treatment on the outcome variable")
        
        ref = None
        
        def placebo_treatment_refuter(model):
            return model.refute_estimate(
                self.estimand,
                self.estimate,
                method_name="placebo_treatment_refuter",
                placebo_type=placebo_type
            )
        def random_common_cause_refuter(model):
            return model.refute_estimate(
                self.estimand,
                self.estimate,
                method_name="random_common_cause"
            )
        def data_subset_refuter(model):
            return model.refute_estimate(
                self.estimand,
                self.estimate,
                method_name="data_subset_refuter",
                subset_fraction=subset_fraction
            )
        
        try:
            match method_name:
                case "placebo_treatment_refuter":
                    ref = placebo_treatment_refuter(self.model)
                
                case "random_common_cause":
                    ref = random_common_cause_refuter(self.model)

                case "data_subset_refuter":
                    ref = data_subset_refuter(self.model)
                
                case "ALL":
                    ref_placebo = placebo_treatment_refuter(self.model)
                    ref_rand_cause = random_common_cause_refuter(self.model)
                    ref_subset = data_subset_refuter(self.model)
                    ref = [ref_placebo, ref_rand_cause, ref_subset]
                    
            if not isinstance(ref, list) and ref.refutation_result['is_statistically_significant']:
                logging.warning("Please make sure to take a revisit the pipeline as the refutation p-val is significant: ", ref.refutation_result['p_value'])
    
            self.est_ref = ref
        
        except Exception as e:
            logging.error(f"Error in refuting estimate: {e}")
            raise
            
        return self.est_ref
    
    def get_all_information(self):
        return {'graph': self.graph, 
                'graph_refutation_res': self.graph_ref,
                'estimand_expression': self.estimand,
                'effect_estimate': self.estimate,
                'estimate_refutation_res': self.est_ref
                }

