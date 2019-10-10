import os.path
import sys
sys.path.append(os.path.join(os.path.dirname(__file__), 'opt_app'))
from time import sleep
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from sqlalchemy import cast, Date, or_
from typing import Dict, Union, Tuple, List, Optional
from helper_functions import create_input_and_output_filepath, load_json, write_json, get_table_for_optimization_id
from evolutionary_toolbox import EAToolbox
from db import Session, engine
from models import Base, OptimizationTask, CalculationTask, OptimizationHistory
from config import CALC_INPUT_EXT, CALC_OUTPUT_EXT, OPTIMIZATION_START, CALCULATION_START, OPTIMIZATION_RUN, \
    CALCULATION_FINISH, OPTIMIZATION_FINISH, MAX_STORING_TIME_OPTIMIZATION_TASKS, OPTIMIZATION_ABORT, DATE_FORMAT


def scalarize_solution(solution):
    return sum(solution)


class OptimizationManager:
    def __init__(self,
                 session,
                 ea_toolbox,
                 optimization_task,
                 optimization_history,
                 calculation_task,
                 debug: bool = False,
                 print_progress: bool = False):
        self.session = session
        self.ea_toolbox = ea_toolbox

        self.ot = optimization_task
        self.oh = optimization_history
        self.ct = calculation_task

        self.debug = debug
        self.debug_counter = 0
        self.print_progress = print_progress

    def debug_manager(self):
        """ Function used for debugging. The main purpose is to let python exit and therefor let docker restart the
        container.

        Returns:
            None - internal counter is modified and at some point this will cause an exit

        """
        self.debug_counter += 1
        if (self.debug_counter % 12) == 0:
            exit()

    @staticmethod
    def create_unique_id() -> uuid4:
        """ Function to create a unique id for calculation jobs

        Returns:
             unique id (uuid4) - a unique id to identify a calculation job

        """
        return uuid4()

    @staticmethod
    def get_weights(data: dict):
        """ Function used to extract objectives from the json that holds the whole optimization task

        Args:
            data (dictionary) - the data as hold by the uploaded json

        Returns:
             objectives (tuple of ints) - the objectives of each function as presented in the json
        """
        return tuple(data["functions"][fun]["objective"] for fun in data["functions"])

    def query_first_starting_optimization_task(self) -> Session.query:
        """

        Returns:
            query - first optimization task in list

        """
        return self.session.query(self.ot)\
            .filter(self.ot.optimization_state == OPTIMIZATION_START).first()

    def query_optimization_task_with_id(self,
                                        optimization_id: uuid4) -> Session.query:
        """

        :param optimization_id:
        :return:
        """

        return self.session.query(self.ot).\
            filter(self.ot.optimization_id == optimization_id).first()

    def query_finished_calculation_tasks(self,
                                         ct_table: CalculationTask,
                                         generation: int) -> Session.query:
        """

        :param ct_table:
        :param generation:
        :return:
        """

        return self.session.query(ct_table).\
            filter(ct_table.generation == generation,
                   ct_table.calculation_state == CALCULATION_FINISH).all()

    def summarize_finished_calculation_tasks(self,
                                             ct_table: CalculationTask,
                                             generation: int) -> List[dict]:
        """

        :param ct_table:
        :param generation:
        :return:
        """

        finished_calculation_tasks = self.query_finished_calculation_tasks(ct_table=ct_table,
                                                                           generation=generation)

        return [load_json(calculation.calcoutput_filepath)
                for calculation in finished_calculation_tasks]

    def await_generation_finished(self,
                                  ct_table: CalculationTask,
                                  optimization_id: uuid4,
                                  generation: int,
                                  total_population: Optional[int] = None):
        """

        :param ct_table:
        :param optimization_id:
        :param generation:
        :param total_population:
        :return:
        """
        if not total_population:
            total_population = self.query_optimization_task_with_id(optimization_id=optimization_id).total_population

        while True:
            current_population = self.session.query(ct_table).\
                filter(ct_table.optimization_id == optimization_id,
                       ct_table.generation == generation,
                       ct_table.calculation_state == CALCULATION_FINISH).count()

            # print(f"ID: {optimization_id}")
            # print(f"Generation: {generation}")
            # print(f"Calculation state: {CALCULATION_FINISH}")

            # print(f"Current number of calculated individuals: {current_population}")

            if current_population == total_population:
                break

    def create_single_calculation_job(self,
                                      ct_table,
                                      optimization_id: uuid4,
                                      individual: List[float],
                                      generation: int = None,
                                      individual_id: int = None) -> None:
        """

        :param ct_table:
        :param optimization_id:
        :param individual:
        :param generation:
        :param individual_id:
        :return:
        """

        optimization_task = self.query_optimization_task_with_id(optimization_id=optimization_id)

        calculation_parameters = {
            "ind_genes": individual
        }

        calculation_id = self.create_unique_id()

        calcinput_filepath, calcoutput_filepath = create_input_and_output_filepath(task_id=calculation_id,
                                                                                   extensions=[CALC_INPUT_EXT,
                                                                                               CALC_OUTPUT_EXT])

        new_calc_task = ct_table(
            author=optimization_task.author,
            project=optimization_task.project,
            optimization_id=optimization_task.optimization_id,
            calculation_id=calculation_id,
            calculation_type=optimization_task.optimization_type,
            calculation_state=CALCULATION_START,  # Set state to start
            generation=generation,
            individual_id=individual_id,
            data_filepath=optimization_task.data_filepath,
            calcinput_filepath=calcinput_filepath,
            calcoutput_filepath=calcoutput_filepath
        )

        write_json(obj=calculation_parameters,
                   filepath=calcinput_filepath)

        # print(f"Generation {generation}: Wrote job json.")

        self.session.add(new_calc_task)
        self.session.commit()

        # print(f"Generation {generation}: Job commited to database.")

    def create_new_calculation_jobs(self,
                                    ct_table: CalculationTask,
                                    optimization_id: uuid4,
                                    generation: int,
                                    population: List[List[float]]):
        """

        :param ct_table:
        :param optimization_id:
        :param generation:
        :param population:
        :return:
        """
        for i, individual in enumerate(population):
            self.create_single_calculation_job(ct_table=ct_table,
                                               optimization_id=optimization_id,
                                               generation=generation,
                                               individual=individual,
                                               individual_id=i)

    def linear_optimization_queue(self,
                                  ct_table,
                                  optimization_id: uuid4,
                                  individual: List[float]):
        """

        :param ct_table:
        :param optimization_id:
        :param individual:
        :return:
        """

        calculation_task = self.session.query(ct_table).last()

        if calculation_task:
            generation = calculation_task.generation + 1
        else:
            generation = 1

        individual_id = 0
        total_population = 1

        individual = list(individual)  # Mystic solver converts solution to a numpy array; we need a list

        self.create_single_calculation_job(ct_table=ct_table,
                                           optimization_id=optimization_id,
                                           generation=generation,
                                           individual=individual,
                                           individual_id=individual_id)

        self.await_generation_finished(ct_table=ct_table,
                                       optimization_id=optimization_id,
                                       generation=generation,
                                       total_population=total_population)

        solution_dict = self.summarize_finished_calculation_tasks(ct_table=ct_table,
                                                                  generation=generation)[0]

        solution = [solution_dict["functions"][fun] for fun in solution_dict["functions"]]

        print(solution)

        optimization_task = self.query_optimization_task_with_id(optimization_id=optimization_id)

        optimization_task.scalar_fitness = scalarize_solution(solution)
        self.session.commit()

        return scalarize_solution(solution)

    def remove_optimization_and_calculation_data(self,
                                                 optimization_id: uuid4) -> None:
        optimization_task = self.query_optimization_task_with_id(optimization_id=optimization_id)

        Path(optimization_task.opt_filepath).unlink()
        Path(optimization_task.data_filepath).unlink()

        individual_ct = get_table_for_optimization_id(CalculationTask, optimization_id)

        calculations = self.session.query(individual_ct).all()

        calculation_files = [(calculation.calcinput_filepath,  calculation.calcoutput_filepath)
                             for calculation in calculations]

        for calcinput_file, calcoutput_file in calculation_files:
            Path(calcinput_file).unlink()
            Path(calcoutput_file).unlink()

    def remove_old_optimization_tasks_and_tables(self):
        now_date = datetime.now().date()

        optimization_tasks = self.session.query(self.ot)\
            .filter(or_(self.ot.optimization_state == OPTIMIZATION_FINISH,
                        self.ot.optimization_state == OPTIMIZATION_ABORT)).all()

        old_optimization_tasks = [task
                                  for task in optimization_tasks
                                  if ((now_date - datetime.strptime(task.publishing_date, DATE_FORMAT)).days >
                                      MAX_STORING_TIME_OPTIMIZATION_TASKS)]

        if old_optimization_tasks:
            for task in old_optimization_tasks:
                individual_ct = get_table_for_optimization_id(CalculationTask, task.optimization_id)
                individual_oh = get_table_for_optimization_id(OptimizationHistory, task.optimization_id)

                Base.metadata.drop_all(tables=[individual_ct, individual_oh], bind=engine)

                self.session.remove(task)
                self.session.commit()

    def manage_evolutionary_optimization(self,
                                         optimization_id: str,
                                         optimization: dict,
                                         ea_toolbox: EAToolbox,
                                         ct_table,
                                         oh_table):
        number_of_generations = optimization["parameters"]["ngen"]
        population_size = optimization["parameters"]["pop_size"]

        population = ea_toolbox.make_population(population_size)

        for generation in range(number_of_generations):
            if self.debug:
                print(f"Generation: {generation}")

            optimization_task = self.query_optimization_task_with_id(optimization_id=optimization_id)

            optimization_task.current_generation = generation + 1  # Table counts to ten
            optimization_task.current_population = 0
            self.session.commit()

            if generation > 0:
                individuals = self.summarize_finished_calculation_tasks(ct_table=ct_table,
                                                                        generation=(generation - 1))
                if self.debug:
                    print(f"Individuals summarized.")

                population = ea_toolbox.optimize_evolutionary(individuals=individuals)

                if self.debug:
                    print(f"Population developed.")

            self.create_new_calculation_jobs(ct_table=ct_table,
                                             optimization_id=optimization_id,
                                             generation=generation,
                                             population=population)

            if self.debug:
                print(f"New jobs created.")

            self.await_generation_finished(ct_table=ct_table,
                                           optimization_id=optimization_id,
                                           generation=generation)

            if self.debug:
                print(f"Jobs finished.")

            individuals = self.summarize_finished_calculation_tasks(ct_table=ct_table,
                                                                    generation=generation)

            if self.debug:
                print(f"Generation summarized.")

            population = ea_toolbox.evaluate_finished_calculations(individuals=individuals)

            if self.debug:
                print(f"Generation evaluated.")

            population = ea_toolbox.select_best_individuals(population=population)

            optimization_history = oh_table(
                author=optimization_task.author,
                project=optimization_task.project,
                optimization_id=optimization_id,
                generation=(generation + 1),
                scalar_fitness=scalarize_solution(ea_toolbox.select_nth_of_hall_of_fame(1)[0].fitness.values)
            )

            self.session.add(optimization_history)
            self.session.commit()

            if self.debug:
                print("Generation selected.")

        return ea_toolbox.select_nth_of_hall_of_fame(optimization["number_of_solutions"])

    def manage_linear_optimization(self,
                                   optimization_id,
                                   optimization,
                                   ea_toolbox):
        def custom_linear_optimization_queue(individual):
            return self.linear_optimization_queue(optimization_id,
                                                  individual)

        solution = ea_toolbox.optimize_linear(solution=optimization["solution"],
                                              function=custom_linear_optimization_queue)

        if self.debug:
            print("Solution linear optimized.")
            print(f"Solution: {solution}")

        return solution

    def manage_any_optimization(self,
                                optimization_id,
                                optimization,
                                ea_toolbox,
                                ct_table,
                                oh_table):
        optimization_task = self.session.query(self.ot)\
            .filter(self.ot.optimization_id == optimization_id).first()

        if optimization_task.optimization_type == "EO":
            return self.manage_evolutionary_optimization(optimization=optimization,
                                                         ea_toolbox=ea_toolbox,
                                                         optimization_id=optimization_id,
                                                         ct_table=ct_table,
                                                         oh_table=oh_table)

        if optimization_task.optimization_type == "LO":
            return self.manage_linear_optimization(optimization=optimization,
                                                   ea_toolbox=ea_toolbox,
                                                   optimization_id=optimization_id)

    def run(self):
        """ Function run is used to keep the manager working constantly. It will work on one optimization only and
        fulfill the job which includes constantly creating jobs for one generation, then after calculation
        summarizing the results and creating new generations with new jobs and finally put the solution back in the
        table and set the optimization to be finished.

        Returns:
            None - output is managed over databases

        """

        while True:
            new_optimization_task = self.query_first_starting_optimization_task()

            if new_optimization_task:
                print(f"Working on task with id: {new_optimization_task.optimization_id}")

                optimization_id = new_optimization_task.optimization_id

                optimization_task = self.query_optimization_task_with_id(optimization_id=optimization_id)
                optimization_task.optimization_state = OPTIMIZATION_RUN
                self.session.commit()

                individual_oh = get_table_for_optimization_id(self.oh, optimization_id)
                individual_ct = get_table_for_optimization_id(self.ct, optimization_id)

                Base.metadata.create_all(bind=engine,
                                         tables=[individual_ct.__table__,
                                                 individual_oh.__table__],
                                         checkfirst=True)

                optimization = load_json(new_optimization_task.opt_filepath)
                data = load_json(new_optimization_task.data_filepath)

                ea_toolbox = self.ea_toolbox(
                    eta=optimization["parameters"]["eta"],
                    bounds=data["individual"]["boundaries"],
                    indpb=optimization["parameters"]["indpb"],
                    cxpb=optimization["parameters"]["cxpb"],
                    mutpb=optimization["parameters"]["mutpb"],
                    weights=self.get_weights(data)
                )

                solution = self.manage_any_optimization(optimization_id=optimization_id,
                                                        optimization=optimization,
                                                        ea_toolbox=ea_toolbox,
                                                        ct_table=individual_ct,
                                                        oh_table=individual_oh)

                optimization_task = self.query_optimization_task_with_id(optimization_id=optimization_id)

                optimization_task.solution = solution
                optimization_task.optimization_state = OPTIMIZATION_FINISH
                self.session.commit()

                # Remove single job properties
                self.remove_optimization_and_calculation_data(optimization_id=optimization_id)

                continue

            self.remove_old_optimization_tasks_and_tables()

            # print("No jobs. Sleeping for 1 minute.")
            # sleep(60)


if __name__ == '__main__':
    sleep(10)

    optimization_manager = OptimizationManager(
        session=Session,
        ea_toolbox=EAToolbox,
        optimization_task=OptimizationTask,
        calculation_task=CalculationTask,
        optimization_history=OptimizationHistory,
        debug=True
    )

    optimization_manager.run()
