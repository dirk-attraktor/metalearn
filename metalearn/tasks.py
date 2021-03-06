
from celery import shared_task
from django.db.models import Q
import numpy
import time
import random
import pickle
from datetime import datetime, timedelta
from scipy.stats import rankdata
from django.db.transaction import on_commit
from django.db.transaction import commit
from django.conf import settings

from billiard import Process
import redis

try:
    redisconnection = redis.StrictRedis(unix_socket_path='/var/run/redis/redis.sock', db=8)
    redisconnection.get("__test")
except:
    redisconnection = redis.StrictRedis(db=8)



def rank(numbers):
    uniq_values = len(set(numbers))
    ranks = rankdata(numbers, method="dense")
    if uniq_values > 1:
        ranks0to1 = ( ranks - 1 ) / ( uniq_values - 1)
    else:
        ranks0to1 = ranks / 2
    return ranks0to1

def rank_and_center(numbers):
    ranks0to1 = rank(numbers)
    ranksMinus1To1 = ( ranks0to1 * 2 ) - 1
    return ranksMinus1To1


# if pid had a gpu lock, release
def gpuunlock(pid):    
    if settings.GPU_ENABLED == False:   
        return True
    for index in range(0, settings.GPU_PARALLEL_PROCESSES):
        r = redisconnection.get("metalearn.gpu.lock.%s" % index)
        if r != None and int(r) == pid:
            print("gpu unlocked")
            redisconnection.delete("metalearn.gpu.lock.%s" % index)
            return

    print("NO GPU UNLOCKED")

# ExperimentSet
@shared_task( bind=True, autoretry_for=(Exception,), exponential_backoff=3, retry_kwargs={'max_retries': 5, 'countdown': 5})
def on_ExperimentSet_created(self, experimentSet_id):
    from .models import ExperimentSet
    from .models import Experiment
    print("on_ExperimentSet_created")
    experimentSet = ExperimentSet.objects.get(id=experimentSet_id) # because autocommit..
    
    combinations = []
    for environment_set in experimentSet.environments_set.all():
        for _ in range(0,environment_set.nr_of_instances):

            for architecture_set in experimentSet.architectures_set.all():
                for _ in range(0,architecture_set.nr_of_instances):

                    for optimiser_set in experimentSet.optimisers_set.all():
                        for _ in range(0,optimiser_set.nr_of_instances):

                            combinations.append( [ environment_set, architecture_set, optimiser_set])


    if len(combinations) > experimentSet.subsettings_Experiments_max:
        combinations = random.sample(combinations, experimentSet.subsettings_Experiments_max)

    for combination in combinations:
        environment_set, architecture_set, optimiser_set = combination
        experiment = Experiment()
        experiment.status = "active"
        experiment.public = experimentSet.public
        experiment.experimentSet = experimentSet
        experiment.environment  = environment_set.environment
        experiment.architecture = architecture_set.architecture
        experiment.optimiser    = optimiser_set.optimiser
        experiment.save()

    ExperimentSet.objects.filter(id = experimentSet_id).update(on_created_executed=True)



@shared_task( bind=True, autoretry_for=(Exception,), exponential_backoff=3, retry_kwargs={'max_retries': 5, 'countdown': 5})
def on_ExperimentSet_done(self, experimentSet_id):
    from .models import ExperimentSet
    from .models import Experiment
    from .models import EpisodeNoisyExecution

    experimentSet = ExperimentSet.objects.get(id=experimentSet_id)
    
    timespend = sum(Experiment.objects.filter(experimentSet_id = experimentSet_id).values_list("timespend",flat=True))
    
    ExperimentSet.objects.filter(id = experimentSet_id).update(on_done_executed=True, timespend=timespend)


# Experiment

@shared_task( bind=True, autoretry_for=(Exception,), exponential_backoff=3, retry_kwargs={'max_retries': 5, 'countdown': 5})
def on_Experiment_created(self, experiment_id, experimentSet_id):
    p = Process(target=_on_Experiment_created, args=(self, experiment_id, experimentSet_id ))
    p.start()
    p.join()
    gpuunlock(p.pid)

def _on_Experiment_created(self, experiment_id, experimentSet_id):
    from .models import Experiment
    from .models import Episode
    print(self, experiment_id, experimentSet_id)
    experiment = Experiment.objects.get(id=experiment_id)

    # create first episode
    episode = Episode()
    episode.environment = experiment.environment
    episode.architecture = experiment.architecture
    episode.optimiser = experiment.optimiser
    episode.experimentSet = experiment.experimentSet
    episode.experiment = experiment
    episode.version = 1
    episode.public = experiment.public

    # init optimiser
    optimiserInstance = experiment.optimiser.getInstance()
    result = optimiserInstance.initialize(episode)  
    if result == "delay":
        print("Delaying Experiment create because no optimiser is available at the moment")
        on_Experiment_created.apply_async([experiment_id, experimentSet_id], countdown=60)
        return 

    episode.weightsNoise, episode.optimiserMetaData, episode.optimiserData, new_factors = result # new_factors = max_noisy_executions, #timespend, #steps, #steps_unrewarded,

    noisyExecutions_max_factor, timespend_factor, steps_factor, steps_unrewarded_factor = new_factors

    eset = episode.experimentSet          
    
    #factors are -1 .. 1 
    h = ( eset.subsettings_EpisodeNoisyExecutions_max           - eset.subsettings_EpisodeNoisyExecutions_min           ) / 2.0
    episode.subsettings_EpisodeNoisyExecutions_max           = eset.subsettings_EpisodeNoisyExecutions_min            +  h + ( h  * noisyExecutions_max_factor      ) 

    h = ( eset.subsettings_EpisodeNoisyExecutions_max_timespend - eset.subsettings_EpisodeNoisyExecutions_min_timespend ) / 2.0
    episode.subsettings_EpisodeNoisyExecutions_max_timespend = eset.subsettings_EpisodeNoisyExecutions_min_timespend  +  h + ( h * timespend_factor  )

    h = ( eset.subsettings_EpisodeNoisyExecutions_max_steps     - eset.subsettings_EpisodeNoisyExecutions_min_steps     ) / 2.0
    episode.subsettings_EpisodeNoisyExecutions_max_steps     = eset.subsettings_EpisodeNoisyExecutions_min_steps      +  h + ( h * steps_factor      )

    h = ( eset.subsettings_EpisodeNoisyExecutions_max_steps     - eset.subsettings_EpisodeNoisyExecutions_min_steps     ) / 2.0
    episode.subsettings_EpisodeNoisyExecutions_max_steps_unrewarded = eset.subsettings_EpisodeNoisyExecutions_min_steps      +  h + ( h * steps_unrewarded_factor )

    episode.save()

    Experiment.objects.filter(id = experiment_id).update(on_created_executed=True)


@shared_task( bind=True, autoretry_for=(Exception,), exponential_backoff=3, retry_kwargs={'max_retries': 5, 'countdown': 5})
def on_Experiment_done(self, experiment_id, experimentSet_id):
    from .models import ExperimentSet
    from .models import Experiment
    from .models import Episode
    from .models import EpisodeNoisyExecution

    ids_fitnesses_timespend = EpisodeNoisyExecution.objects.filter(experiment_id = experiment_id).values_list('id',"fitness","timespend").distinct()
    fitnesses = [x[1] for x in ids_fitnesses_timespend]
    timesspend  = [x[2] for x in ids_fitnesses_timespend]

    experiment = Experiment.objects.get(id=experiment_id)
    # calc episode average/sum values
    experiment.timespend   = sum(timesspend)
    experiment.fitness_min   = min(fitnesses)
    experiment.fitness_max   = max(fitnesses)
    experiment.fitness_avg   = numpy.mean(fitnesses)
    experiment.fitness_median =  numpy.median(fitnesses)
    experiment.fitness_top10 =  numpy.mean(sorted(fitnesses, reverse=True)[0:10])
    experiment.save()

    latest_episode = [e for e in experiment.episodes.all()][-1]
    optimiserInstance = latest_episode.optimiser.getInstance()
    optimiserInstance.reward(latest_episode)

    experiments_to_go = Experiment.objects.filter(experimentSet_id = experimentSet_id).filter(~Q(status = "done")).count()
    if experiments_to_go == 0:
        experimentSet_done = ExperimentSet.objects.filter(id = experimentSet_id).filter(~Q(status = "done")).update(status="done")
        if experimentSet_done == 1:
            on_commit(lambda: on_ExperimentSet_done.delay(experimentSet_id))

    Experiment.objects.filter(id = experiment_id).update(on_done_executed=True)


# Episode

@shared_task( bind=True, autoretry_for=(Exception,), exponential_backoff=3, retry_kwargs={'max_retries': 5, 'countdown': 5})
def on_Episode_created(self, episode_id, experiment_id, experimentSet_id):
    from .models import Episode
    from .models import EpisodeNoisyExecution

    episode = Episode.objects.get(id=episode_id)

    count = episode.noisyExecutions.count()

    for number in range(count,episode.subsettings_EpisodeNoisyExecutions_max):
        episodeNoisyExecution = EpisodeNoisyExecution()
        episodeNoisyExecution.environment = episode.environment
        episodeNoisyExecution.architecture = episode.architecture
        episodeNoisyExecution.optimiser = episode.optimiser
        episodeNoisyExecution.experimentSet = episode.experimentSet
        episodeNoisyExecution.experiment = episode.experiment
        episodeNoisyExecution.episode = episode
        episodeNoisyExecution.number = number
        episodeNoisyExecution.save()

    Episode.objects.filter(id = episode_id).update(on_created_executed=True)

@shared_task( bind=True, autoretry_for=(Exception,), exponential_backoff=3, retry_kwargs={'max_retries': 5, 'countdown': 5})
def on_Episode_done(self, episode_id, experiment_id, experimentSet_id):
    p = Process(target=_on_Episode_done, args=(self, episode_id, experiment_id, experimentSet_id ))
    p.start()
    p.join()
    gpuunlock(p.pid)

def _on_Episode_done(self, episode_id, experiment_id, experimentSet_id):
    from .models import Episode
    from .models import Experiment
    from .models import ExperimentSet
    from .models import EpisodeNoisyExecution

    current_episode = Episode.objects.get(id=episode_id)

    # calc fitness ranks/norm and so on
    ids_fitnesses_timespend = current_episode.noisyExecutions.all().values_list('id',"fitness","timespend").distinct()
    fitnesses = [x[1] for x in ids_fitnesses_timespend]
    timesspend  = [x[2] for x in ids_fitnesses_timespend]

    fitnesses_rank  = rank(fitnesses)

    m = max(numpy.abs(fitnesses))
    if m == 0:
        fitnesses_scaled = fitnesses
    else:
        fitnesses_scaled = fitnesses / m 

    s = numpy.std(fitnesses)
    if s == 0:
        fitnesses_norm   = fitnesses - numpy.mean(fitnesses)
    else:
        fitnesses_norm   = (fitnesses - numpy.mean(fitnesses)) / s

    m = max(numpy.abs(fitnesses_norm))
    if m == 0:
        fitnesses_norm_scaled = fitnesses_norm 
    else:
        fitnesses_norm_scaled = fitnesses_norm / m

    for i in range(0,len(fitnesses)):
        EpisodeNoisyExecution.objects.filter(id=ids_fitnesses_timespend[i][0]).update(
            fitness_rank   = fitnesses_rank[i], 
            fitness_scaled = fitnesses_scaled[i], 
            fitness_norm   = fitnesses_norm[i],
            fitness_norm_scaled = fitnesses_norm_scaled[i],
        )

    # calc episode average/sum values
    current_episode.timespend   = sum(timesspend)
    current_episode.fitness_min   = min(fitnesses)
    current_episode.fitness_max   = max(fitnesses)
    current_episode.fitness_avg   = numpy.mean(fitnesses)
    current_episode.fitness_median =  numpy.median(fitnesses)
    current_episode.fitness_top10 =  numpy.mean(sorted(fitnesses,reverse=True)[0:10])
    current_episode.save()

    # check if episodes are finished
    episodes_finished = Episode.objects.filter(experiment_id = experiment_id).filter(status = "done").count()
    max_Episodes = ExperimentSet.objects.get(id=experimentSet_id).subsettings_Episodes_max
    if episodes_finished >= max_Episodes:
        experiment_done = Experiment.objects.filter(id = experiment_id).filter(~Q(status = "done")).update(status="done")
        if experiment_done == 1:
            on_commit(lambda: on_Experiment_done.delay(experiment_id, experimentSet_id))

        # clean hd space
        current_episode.weightsNoise = numpy.array([])
        current_episode.optimiserData = pickle.dumps({})
        
    else:
        # create next episode
        next_episode = Episode()
        next_episode.environment = current_episode.environment
        next_episode.architecture = current_episode.architecture
        next_episode.optimiser = current_episode.optimiser
        next_episode.experimentSet = current_episode.experimentSet
        next_episode.experiment = current_episode.experiment
        next_episode.version = current_episode.version + 1
        next_episode.public = current_episode.public

        # run optimiser
        optimiserInstance = next_episode.optimiser.getInstance()
        next_episode.weightsNoise, next_episode.optimiserMetaData, next_episode.optimiserData, new_factors = optimiserInstance.optimise(current_episode)
        noisyExecutions_max_factor, timespend_factor, steps_factor, steps_unrewarded_factor = new_factors

        # clean hd space
        current_episode.weightsNoise = numpy.array([])
        current_episode.optimiserData = pickle.dumps({})

        eset = next_episode.experimentSet          
        
        #factors are -1 .. 1 
        h = ( eset.subsettings_EpisodeNoisyExecutions_max           - eset.subsettings_EpisodeNoisyExecutions_min           ) / 2.0
        next_episode.subsettings_EpisodeNoisyExecutions_max           = eset.subsettings_EpisodeNoisyExecutions_min            +  h + ( h  * noisyExecutions_max_factor      ) 

        h = ( eset.subsettings_EpisodeNoisyExecutions_max_timespend - eset.subsettings_EpisodeNoisyExecutions_min_timespend ) / 2.0
        next_episode.subsettings_EpisodeNoisyExecutions_max_timespend = eset.subsettings_EpisodeNoisyExecutions_min_timespend  +  h + ( h * timespend_factor  )

        h = ( eset.subsettings_EpisodeNoisyExecutions_max_steps     - eset.subsettings_EpisodeNoisyExecutions_min_steps     ) / 2.0
        next_episode.subsettings_EpisodeNoisyExecutions_max_steps     = eset.subsettings_EpisodeNoisyExecutions_min_steps      +  h + ( h * steps_factor      )

        h = ( eset.subsettings_EpisodeNoisyExecutions_max_steps     - eset.subsettings_EpisodeNoisyExecutions_min_steps     ) / 2.0
        next_episode.subsettings_EpisodeNoisyExecutions_max_steps_unrewarded = eset.subsettings_EpisodeNoisyExecutions_min_steps      +  h + ( h * steps_unrewarded_factor )

        next_episode.save()

    Episode.objects.filter(id = episode_id).update(on_done_executed=True)



# EpisodeNoisyExecution
@shared_task( bind=True, autoretry_for=(Exception,), exponential_backoff=3, retry_kwargs={'max_retries': 5, 'countdown': 5})
def on_NoisyExecution_created(self, noisyExecution_id, episode_id, experiment_id, experimentSet_id):
    EpisodeNoisyExecution.objects.filter(id = noisyExecution_id).update(on_created_executed=True)


@shared_task( bind=True, autoretry_for=(Exception,), exponential_backoff=3, retry_kwargs={'max_retries': 5, 'countdown': 5})
def on_NoisyExecution_done(self, noisyExecution_id, episode_id, experiment_id, experimentSet_id):
    from .models import EpisodeNoisyExecution
    from .models import Episode
    
    episode = Episode.objects.get(id=episode_id)
    noisyExecutions_done = EpisodeNoisyExecution.objects.filter(episode_id = episode_id).filter(status = "done").count()

    if noisyExecutions_done >= episode.subsettings_EpisodeNoisyExecutions_max:

        episode_done = Episode.objects.filter(id = episode_id).filter(~Q(status = "done")).update(status="done")

        if episode_done == 1:
            on_Episode_done.delay(episode_id, experiment_id, experimentSet_id)

    EpisodeNoisyExecution.objects.filter(id = noisyExecution_id).update(on_done_executed=True)



# CRON

@shared_task
def cron_clean_locked_hanging():
    from .models import EpisodeNoisyExecution
    
    tdiff = datetime.now() - timedelta(minutes=5)
    noisyExecution_hangs = EpisodeNoisyExecution.objects.filter(public = True, status = "locked", updated__lt = tdiff)
    for noisyExecution_hang in noisyExecution_hangs:
        noisyExecution_hang.locked = False
        noisyExecution_hang.client = ""
        noisyExecution_hang.lock = ""
        noisyExecution_hang.status = "idle"
        noisyExecution_hang.save()
    

def clean_hd():
    max_diskspace = 200   # in gigabyte
    used_diskspace = 190  # in gigabyte
    
    percent_free = 10    # keep 10% of max available
    max_diskspace = max_diskspace - (max_diskspace / 100.0 * percent_free)

    if used_diskspace > max_diskspace:
        print("Must clean diskspace")
        episodes = Episode.objects.filter(status="done", hasFolder=True).filter(~Q(next=None)).order_by("updated")[100:]
        for episode in episodes:
            episode.hasFolder = False
            episode.save()



'''
@shared_task
def on_OptimiserTraining_created(optimiserTraining_id):
    from .models import OptimiserTraining
    from .models import ExperimentSet
    from .models import Experiment

    optimiserTraining = OptimiserTraining.objects.get(id=optimiserTraining_id) 
    
    envs = []
    archs = []
    for environment_set in optimiserTraining.environments_set.all():
        for _ in range(0,environment_set.nr_of_instances):
            envs.append(environment_set.environment)
            for architecture_set in optimiserTraining.architectures_set.all():
                for _ in range(0,architecture_set.nr_of_instances):
                    archs.append(architecture_set.architecture)

    if len(envs) > optimiserTraining.max_parallel_envs_per_episode:
        envs = random.sample(envs, optimiserTraining.max_parallel_envs_per_episode)
    if len(archs) > optimiserTraining.max_parallel_archs_per_episode:
        archs = random.sample(archs, optimiserTraining.max_parallel_archs_per_episode)

    experimentSet = ExperimentSet()

    experimentSet.public = True
    experimentSet.name = ""
    experimentSet.description = ""
    experimentSet.status = "active"

    experimentSet.optimiserTraining = optimiserTraining 
    experimentSet.optimiserTraining_episodeNr = 1

    experimentSet.environments = envs 
    experimentSet.architectures = archs
    experimentSet.optimisers = optimiserTraining.optimiser

    experimentSet.max_Episodes = 2
    experimentSet.max_Experiments = optimiserTraining.episodeNoisyExecutions_count_max

    experimentSet.episodeNoisyExecutions_count_min = 5
    experimentSet.episodeNoisyExecutions_count_max = 500
    experimentSet.episodeNoisyExecution_timespend_min = 10
    experimentSet.episodeNoisyExecution_timespend_max = 120
    experimentSet.episodeNoisyExecution_steps_min = 10
    experimentSet.episodeNoisyExecution_steps_max = 10000
    experimentSet.save()


def on_OptimiserTraining_EpisodeDone(experimentSet_id):
    from .models import OptimiserTraining
    from .models import ExperimentSet
    from .models import Experiment

    last_experimentSet = ExperimentSet.objects.get(id=experimentSet_id) 
    optimiserTraining = last_experimentSet.optimiserTraining

    archs = [ x.architecture for x in last_experimentSet.architectures.all() ]    
    ac = optimiserTraining.max_parallel_archs_change_per_episode
    while len(archs) > 0 and ac > 0:
        del archs[random.randint(0,len(archs)-1)]
        ac -= 1

    envs  = [ x.environment  for x in last_experimentSet.environments.all()  ]
    ec = optimiserTraining.max_parallel_envs_change_per_episode
    while len(envs) > 0 and ec > 0:
        del envs[random.randint(0,len(envs)-1)]
        ec -= 1

    n_envs = []
    n_archs = []
    for environment_set in optimiserTraining.environments_set.all():
        for _ in range(0,environment_set.nr_of_instances):
            n_envs.append(environment_set.environment)
            for architecture_set in optimiserTraining.architectures_set.all():
                for _ in range(0,architecture_set.nr_of_instances):
                    n_archs.append(architecture_set.architecture)

    diff = optimiserTraining.max_parallel_envs_per_episode - len(envs)
    while diff > 0:
        envs.append(random.choice(n_envs))
        diff -= 1 

    diff = optimiserTraining.max_parallel_envs_per_episode - len(archs)
    while diff > 0:
        archs.append(random.choice(n_archs))
        diff -= 1 

    experimentSet = ExperimentSet()

    experimentSet.public = True
    experimentSet.name = ""
    experimentSet.description = ""
    experimentSet.status = "active"

    experimentSet.optimiserTraining = optimiserTraining 
    experimentSet.optimiserTraining_episodeNr = last_experimentSet.optimiserTraining_episodeNr + 1 

    experimentSet.environments = envs 
    experimentSet.architectures = archs
    experimentSet.optimisers = optimiserTraining.optimiser

    experimentSet.max_Episodes = 2
    experimentSet.max_Experiments = optimiserTraining.episodeNoisyExecutions_count_max

    experimentSet.episodeNoisyExecutions_count_min = 5
    experimentSet.episodeNoisyExecutions_count_max = 500
    experimentSet.episodeNoisyExecution_timespend_min = 10
    experimentSet.episodeNoisyExecution_timespend_max = 120
    experimentSet.episodeNoisyExecution_steps_min = 10
    experimentSet.episodeNoisyExecution_steps_max = 10000
    experimentSet.save()


@shared_task
def on_OptimiserTraining_done(experimentSet_id):
    from .models import ExperimentSet
    from .models import Experiment
    timespend = 0.0
    for ep in Experiment.objects.filter(experimentSet_id = experimentSet_id):
        timespend += ep.timespend
    experiment = ExperimentSet.objects.get(id=experimentSet_id)
    experiment.timespend = timespend
    experiment.save()



'''